"""Agent loop. Port of Onyx run_llm_loop, slim edition.

Cycle: run_llm_step -> run_tool_calls -> append assistant+tool messages.
Repeat until no tool calls or cycles exhaust.

Two deliberate deviations from Onyx (both forced by Groq gpt-oss behavior):
- Tools are NEVER stripped on the final cycle. Groq emits an error chunk
  ("Tool choice is none, but model called a tool") when history contains
  tool exchanges but the request carries no tools. Instead the last cycle
  keeps tools + a "answer now" reminder.
- A missing final answer falls back to an extractive summary of the
  gathered search docs, so the loop always returns something useful.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator

from . import config as app_config
from .llm import LLM
from .llm_step import (
    extract_tool_calls_from_text,
    has_textual_tool_calls,
    run_llm_step,
    strip_textual_tool_calls,
    strip_unknown_citations,
)
from .models import (
    ChatMessageSimple,
    LlmStepResult,
    MessageType,
    Packet,
    SearchDoc,
    ToolCallResult,
    ToolCallSimple,
    ToolResponse,
)
from .prompts import OPEN_URL_REMINDER
from .tools.interface import Tool
from .tools.open_url import OpenURLTool
from .tools.runner import run_tool_calls
from .utils import setup_logger
logger = setup_logger("custom_chat.loop")

FINAL_ANSWER_REMINDER = (
    "No more tool calls after this. Provide the final answer now using the "
    "tool results above. Cite web documents with their [N] numbers."
)

# "…조회해드릴게요" 하고 턴을 끝내는 패턴. 툴을 하나도 부르지 않은 채 미래형
# 으로 끝나면 사용자에게 전달되는 답이 없다. 프롬프트로 막으면 새지 않으므로
# 결정적으로 검사해 재촉한다. (원 질문 그대로: "왜 조회해볼게요 하고 끝남?")
# 1인칭 예고만 겨냥한다. "확인해 보세요"(권유)는 제외 — 어절이 다르다.
# 실측에서 동사가 "정리해 드리겠습니다" "확인해볼게요" "찾을게요" 처럼 제각각이라
# 열거가 실제 문장을 놓쳤다. 동사 + 좁은 창 + 의도 어미 로 판정한다.
_PROMISE_VERB = r"(?:조회|검색|확인|정리|살펴|탐색|안내|찾|모아|나열|작성|제시|전달|드릴|보여|알려|답변|체가)"
_PROMISE_END = (
    r"(?:하겠습니다|하겠|할게요|할까|하러|해두겠|거든요"
    r"|보겠습니다|보겠|볼게요|보게요|보러|보았고|봤고"
    r"|드릴게요|드리겠습니다|드릴까|드린 뒤"
    r"|알려줄게요|알려드릴게요|알려드리겠습니다|전달드리겠습니다|보여드리겠습니다"
    r"|제시해?\s*드릴|정리해?\s*드릴|확인해?\s*드릴|안내해?\s*드릴"
    r"|을게요|을까|을까\s*합니다|어볼게요|어보겠습니다|어보려고)"
)
PROMISE_RE = re.compile(_PROMISE_VERB + r"[^.!?\n]{0,10}?" + _PROMISE_END)
# 실제 답이라면 반드시 따라붙는 데이터 링크. 이게 있으면 "답"으로 본다.
DATA_LINK_MARKERS = ("data.g2b.go.kr", "goods.g2b.go.kr")
PROMISE_MAX_NUDGES = 2
TOOL_REQUIRED_REMINDER = (
    "방금 답변은 도구 호출 '예고'이고 실제 답이 아니다. "
    "이제 반드시 mcp_jodal_search_reports를 실제로 호출해라. "
    "호출 결과가 없으면 보고서명+링크를 넣어 답하고, "
    "어떤 예고 문장도 출력하지 마라."
)


def _trim(text: str | None, limit: int = 60) -> str:
    flat = " ".join((text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _last_sentence(text: str) -> str:
    parts = [p.strip() for p in re.split(r"[.!?。\n]+", text) if p.strip()]
    return parts[-1] if parts else text.strip()


# 답 앞부분의 "할게요" 나레이티브. ("…정리해 드릴게요.", "…검색하겠습니다.")
# 결과는 뒤따르는데 앞에 붙는 진행 폭로는 읽는 사람에게 노이즈다.
NARRATION_VERBS = (
    "정리", "확인", "조회", "검색", "살펴", "탐색", "찾아", "알아", "정리",
    "모아", "나열", "안내", "작성", "제공",
)
# 어미 판정은 PROMISE_RE 하나로 통일한다. 여기서 따로 어미 목록을 두면
# "찾아보겠습니다" 같은 형태를 놓친다(실측: NARRATION_TAILS 에裸 "보겠습니다" 가
# 없어서 나레이티브가 그대로 노출됨). PROMISE_RE 는 동사+어미를 함께 본다.
# 앞 N개 문장만 본다. 본문 중간이 "~입니다" 류는 지우면 안 된다.
NARRATION_MAX_LEAD = 2
# 0: 나레이티브만 있던 사이클은 통째로 지운다. 앞 사이클의 예고("…검색해볼게요.")
# 는 다음 사이클 내용과 별개 패킷이라, 나머지 길이 조건을 두면 영영 못 지운다.
NARRATION_MIN_REMAINDER = 0


def strip_leading_narration(answer: str | None) -> str:
    """Drop narration sentences from the first few sentences of the answer.

    Position-independent within the lead: real answers often open with a short
    framing sentence ("드론/용접기 후보가 명확합니다.") and carry the progress
    line in sentence two. Only sentences matching both a narration verb and a
    future-tense tail are dropped, and only while a real remainder survives.
    """
    if not answer:
        return answer or ""
    lead: list[str] = []
    rest = answer
    examined = 0
    dropped = 0
    while examined < NARRATION_MAX_LEAD:
        m = re.match(r"^(\s*)([^\n.!?。]{4,80})([.!?。])(\s*)", rest)
        if not m:
            break
        examined += 1
        sentence = m.group(2)
        is_narration = bool(PROMISE_RE.search(sentence))
        if is_narration:
            dropped += 1
        else:
            lead.append(m.group(1) + sentence + m.group(3) + m.group(4))
        rest = rest[m.end():]
    if not dropped:
        return answer
    candidate = "".join(lead) + rest
    if len(candidate.strip()) < NARRATION_MIN_REMAINDER:
        return answer
    return candidate


def _is_promise_only(answer: str | None) -> bool:
    """True when the text promises a lookup instead of delivering a result."""
    if not answer:
        return False
    text = answer.strip()
    if not text:
        return False
    if any(m in text for m in DATA_LINK_MARKERS):
        return False  # 데이터 링크가 있으면 실제 답
    return bool(PROMISE_RE.search(_last_sentence(text)))


def _is_all_narration(answer: str | None) -> bool:
    """Every sentence is a progress line — nothing usable was produced."""
    if not answer:
        return False
    text = answer.strip()
    if not text or any(m in text for m in DATA_LINK_MARKERS):
        return False
    sentences = [s.strip() for s in re.split(r"[.!?。\n]+", text) if s.strip()]
    if not sentences:
        return False
    return all(
        any(v in s for v in NARRATION_VERBS) and PROMISE_RE.search(s) for s in sentences
    )


def strip_trailing_narration(answer: str | None) -> str:
    """Drop a trailing 'I will go check' sentence so the answer ends on content."""
    if not answer:
        return answer or ""
    text = answer.rstrip()
    for _ in range(2):
        m = re.search(r"([^\n.!?。]{4,90})([.!?。])\s*$", text)
        if not m:
            break
        sentence = m.group(1)
        if not PROMISE_RE.search(sentence):
            break
        head = text[: m.start()].rstrip()
        if not head:
            break
        text = head
    return text


def _clean_pseudo_tool_call_packets(
    packets: list[Packet],
    tool_defs: list[dict],
    allowed_urls: set[str] | None = None,
    known_ids: set[str] | None = None,
    strip_ids_always: bool = False,
    tools_ran: bool = False,
) -> list[Packet]:
    """Answer hygiene on the streamed text: drop `[tool: NAME]` syntax, leading
    "I will look it up" narration, links the tools never produced, unsupported
    report ids, and stray <think> tags.

    Some models answer a tool call in the text channel, open with a progress
    line, invent plausible URLs, and recall report ids wrongly. The text is
    already split into message_delta packets by the time we can tell, so
    re-join the deltas, clean, and re-emit as one delta.
    """
    delta_idx = [i for i, p in enumerate(packets) if p.type == "message_delta"]
    if not delta_idx:
        return packets
    joined = "".join(str(packets[i].data.get("content", "")) for i in delta_idx)
    cleaned = joined
    if has_textual_tool_calls(joined, tool_defs):
        cleaned = strip_textual_tool_calls(cleaned, tool_defs)
    if tools_ran:
        cleaned, dropped = strip_fabricated_links(cleaned, allowed_urls or set())
        if dropped:
            logger.warning("Dropped fabricated link(s) in stream: %s", dropped)
    if known_ids is not None or strip_ids_always:
        if strip_ids_always:
            cleaned, hidden = hide_report_ids(cleaned)
            if hidden:
                logger.info("Hid report id(s) in stream: %s", hidden)
        else:
            cleaned, dropped_ids = strip_unknown_report_ids(cleaned, known_ids)
            if dropped_ids:
                logger.warning("Dropped unsupported report id(s) in stream: %s", dropped_ids)
    cleaned = strip_think_tags(cleaned)
    cleaned = strip_leading_narration(cleaned)
    cleaned = strip_trailing_narration(cleaned)
    if cleaned == joined:
        return packets
    first = delta_idx[0]
    out = list(packets)
    template = packets[first]
    for i in delta_idx[1:]:
        out[i] = None  # type: ignore[call-overload]
    out[first] = Packet(
        turn_index=template.turn_index,
        tab_index=template.tab_index,
        type="message_delta",
        data={"content": cleaned},
    )
    return [p for p in out if p is not None]  # type: ignore[misc]


class LoopResult:
    def __init__(self) -> None:
        self.answer: str | None = None
        self.citation_docs: dict[int, SearchDoc] = {}
        self.tool_calls: list[ToolCallResult] = []
        # 툴 응답에서 실제로 등장한 URL. 답에 든 링크는 여기 있는 것만 허용한다.
        self.tool_urls: set[str] = set()
        # 툴 응답에 실제로 등장한 report_id. 없으면 답의 5자리 숫자는 모두 무근거.
        self.tool_report_ids: set[str] = set()
        self.saw_tool_payload = False


def user_mentions_report_id(user_text: str | None) -> bool:
    """True when the user typed a report-id-shaped token themselves."""
    if not user_text:
        return False
    return any(_looks_like_report_id(t) for t in _REPORT_ID_RE.findall(user_text))


def _is_rate_limit(error: Exception) -> bool:
    name = type(error).__name__.lower()
    msg = str(error).lower()
    return (
        "ratelimit" in name
        or "rate_limit" in msg
        or "rate limit" in msg
        or "429" in msg
        or "tpm" in msg
    )


# 역슬래시를 반드시 제외한다. 툴 페이로드는 JSON 이스케이프되어 URL 뒤에 `\"`가
# 붙는데(예: ...UI-ADOXFA-105R\"), 이게 제외되지 않으면 정상 URL이 뒤에 역슬래시
# 붙은 채 캡처되어 "모델이 쓴 깨끗한 URL"과 매칭이 실패하고 위조 링크로 오판된다.
_URL_RE = re.compile(r"https?://[^\s)\]\"'<>\\]+")
_URL_TRAIL = ".,);]\\"
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
# 링크만 남고 의미가 없는 줄. 가짜 URL을 지우면 "- 바로 열기" 처럼 되돌아오는데,
# 그건 아무짝도 못 쓰는 줄이므로 줄째로 없앤다.
_DANGLING_LINK_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*+]\s*|\d+[.)]\s*)?(?:#{1,6}\s*)?"
    r"(?:바로 ?열기|바로 ?보기|여기|링크|자세히 ?보기|접속|이동|열기|보기|링크보기)[ \t]*:?[ \t]*$",
    re.MULTILINE,
)


def collect_tool_urls(payloads: list[str]) -> set[str]:
    return {u.rstrip(_URL_TRAIL) for p in payloads for u in _URL_RE.findall(p or "")}


def strip_fabricated_links(
    answer: str | None, allowed: set[str] | None
) -> tuple[str, list[str]]:
    """Drop links the tools never produced.

    The models happily invent a plausible-looking URL (observed:
    https://jodal.go.kr/reports/00262 and https://jodal.orla.cc/reports/, neither
    of which exists) when they answer from memory instead of tool output. A
    wrong link sends the user to a dead page and is worse than no link, so only
    tool-sourced URLs survive. `allowed` empty means "no link is backed".
    """
    if not answer:
        return answer or [], []
    if not allowed:
        # 툴이 URL을 하나도 내지 않은 턴(=허용집합 0)에서는 답의 모든 링크가
        # 무근거다. 예: value_lookup 은 URL을 주지 않는데 모델이
        # https://jodal.orla.cc/reports/ 같은 주소를 지어냈다(실측).
        allowed = set()
    dropped: list[str] = []

    def _repl(m: re.Match[str]) -> str:
        url = m.group(2)
        if url.rstrip(_URL_TRAIL) in allowed:
            return m.group(0)
        dropped.append(url)
        return m.group(1) or ""

    cleaned = _MD_LINK_RE.sub(_repl, answer)
    if dropped:
        cleaned = _DANGLING_LINK_LINE_RE.sub("", cleaned)
        # 링크가 모두 사라진 빈 머리말/꼬리말 정리
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip("\n")
    return cleaned, dropped


# 리포팅/상품 코드처럼 생긴 5자리 숫자. 툴 결과에 없으면 지어낸 것이다.
# 실측: 툴을 아예 부르지 않고 기억으로 답한 턴이 "수요기관별 계약납품요구
# 실적통계 = 00153" 처럼 엉뚱한 ID를 지어냈다(실제 00262). ID를 감추라는
# 규칙만으로는 막히지 않으므로, 근거 없는 ID는 결정적으로 뺀다.
# 실제 report_id 는 00001~00999 — 앞자리 0인 5자리여야 한다. 정수값으로 범위를
# 재면 "00153" 이 153 으로 줄지므로 문자열 모양으로 판정한다.
_REPORT_ID_RE = re.compile(r"(?<![\d])(\d{5})(?![\d])")
# 모델이 스스로 closing 태그를 텍스트로 뱉는 경우 (추론이 답변으로 새는 증상)
_THINK_TAG_RE = re.compile(r"<\s*/?\s*(?:think|thinking|thought|reason)\s*>", re.IGNORECASE)


def _looks_like_report_id(tok: str) -> bool:
    # 실제 report_id 범위는 00001~00999 → "00"으로 시작하는 5자리.
    # "01000" 같은 5자리 수는 ID가 아니라 일반 숫자다.
    return len(tok) == 5 and tok[:2] == "00"


# URL 본문(마크다운 링크 대상 +裸 URL). report_id 를 지울 때 건드리지 않을 구간.
# get_report_detail 의 direct 링크가 ?g2bOpen=00262 형태라, 여기까지 regex 를
# 그냥 돌리면 링크가 ?g2bOpen= 로 깨진다(실측). 사용자에게 dead link 가 나가는
# 쪽이 내부 ID 노출보다 나쁘다. report_id 는 어차피 링크 안에 이미 있다.
_URL_SPAN_RE = re.compile(r"\]\(\s*https?://[^)\s]*\s*\)|https?://[^\s)\]<>\"']+")


def _sub_outside_urls(
    text: str, repl: Callable[[re.Match[str]], str]
) -> str:
    """URL 구간은 그대로 두고, 바깥 텍스트에만 repl 을 적용한다."""
    out: list[str] = []
    last = 0
    for m in _URL_SPAN_RE.finditer(text):
        out.append(_REPORT_ID_RE.sub(repl, text[last : m.start()]))
        out.append(m.group(0))
        last = m.end()
    out.append(_REPORT_ID_RE.sub(repl, text[last:]))
    return "".join(out)


def collect_tool_report_ids(payloads: list[str]) -> set[str]:
    return {m for p in payloads for m in _REPORT_ID_RE.findall(p or "")}


def strip_unknown_report_ids(
    answer: str | None, known: set[str] | None, strip_all: bool = False
) -> tuple[str, list[str]]:
    """Remove report-id-shaped tokens from the answer.

    `known is None` means no tool ran this turn, so every id-shaped token is
    unsupported. `known` set keeps the ids the tools actually returned.
    `strip_all` removes them regardless of provenance (used to hide ids from
    the reader). Non report-id numbers (5-digit counts, 10-digit item
    classification codes, years) are always left alone.

    ★ URL 안쪽은 건드리지 않는다. get_report_detail 의 direct 링크가
    `?g2bOpen=00262` 형식이라, 전체 텍스트에 regex 를 돌리면 링크가
    `?g2bOpen=` 로 깨진다(실측). 내부 ID 를 감출 목적은 링크 안의 ID 가
    자연스럽게 숨겨진다는 것이고, 그걸 바깥에서까지 지우면 dead link 만 남는다.
    """
    if not answer:
        return answer or [], []
    dropped: list[str] = []

    def _repl(m: re.Match[str]) -> str:
        tok = m.group(1)
        if not _looks_like_report_id(tok):
            return m.group(0)
        if not strip_all and known is not None and tok in known:
            return m.group(0)
        dropped.append(tok)
        return ""

    cleaned = _sub_outside_urls(answer, _repl)
    if dropped:
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned, dropped


def hide_report_ids(answer: str | None) -> tuple[str, list[str]]:
    """Strip every report id from the answer, tool-sourced or not.

    The user asked for ids to stay internal: they add nothing for a reader and
    the number is already inside the link. The models keep copying them out of
    the MCP payloads, so a prompt rule alone did not hold. The one exception is
    when the user brought an id up themselves — then repeating it is the point,
    so the caller skips this.
    """
    return strip_unknown_report_ids(answer, known=set(), strip_all=True)


def strip_think_tags(answer: str | None) -> str:
    if not answer:
        return answer or ""
    return _THINK_TAG_RE.sub("", answer)


def run_loop(
    history: list[ChatMessageSimple],
    tools: list[Tool],
    llm: LLM,
    max_cycles: int | None = None,
) -> Iterator[Packet | LoopResult]:
    """Generator: yields Packets; final LoopResult arrives as StopIteration value."""
    max_cycles = max_cycles or app_config.MAX_LLM_CYCLES
    result = LoopResult()
    citation_start = 1
    fallback_attempted = False

    answer: str | None = None
    last_answer: str | None = None
    pending_calls = False
    promise_nudges = 0
    # 이번 턴의 사용자 원문. 사용자가 report_id 를 직접 물어봤는지 판별한다.
    latest_user_text = next(
        (
            m.message
            for m in reversed(history)
            if getattr(m, "message_type", None) == MessageType.USER
            and isinstance(getattr(m, "message", None), str)
        ),
        "",
    )
    # jodal 툴이 없는 구성에서는 "조회해볼게요"를 재촉할 대상이 없다.
    has_jodal_tools = any(
        getattr(t, "display_name", "") and "jodal" in getattr(t, "name", "")
        for t in tools
    )
    cycle = 0
    for cycle in range(max_cycles):
        is_last = cycle == max_cycles - 1
        step_history = history
        if is_last and tools:
            step_history = [
                *history,
                ChatMessageSimple(
                    message=FINAL_ANSWER_REMINDER, message_type=MessageType.USER
                ),
            ]
        tool_defs = [t.tool_definition() for t in tools]

        try:
            step_result, step_packets = run_llm_step(
                history=step_history,
                tool_definitions=tool_defs,
                tool_choice="auto",
                llm=llm,
                turn_index=cycle,
            )
        except Exception as e:
            logger.warning("LLM step failed on cycle %d: %s", cycle, e)
            if _is_rate_limit(e) and cycle == 0:
                # Tiny TPM tiers need a beat before the first retry.
                time.sleep(5)
                try:
                    step_result, step_packets = run_llm_step(
                        history=step_history,
                        tool_definitions=tool_defs,
                        tool_choice="auto",
                        llm=llm,
                        turn_index=cycle,
                    )
                except Exception as e2:
                    logger.error("LLM step retry failed: %s", e2)
                    yield Packet(
                        turn_index=cycle,
                        type="error",
                        data={"error": f"LLM step failed: {e2}"},
                    )
                    break
            else:
                yield Packet(
                    turn_index=cycle,
                    type="error",
                    data={"error": f"LLM step failed: {e}"},
                )
                break
        step_packets = _clean_pseudo_tool_call_packets(
            step_packets,
            tool_defs,
            result.tool_urls | collect_tool_urls([latest_user_text]),
            result.tool_report_ids if result.saw_tool_payload else None,
            strip_ids_always=not user_mentions_report_id(latest_user_text),
            tools_ran=result.saw_tool_payload,
        )
        for p in step_packets:
            yield p

        if not fallback_attempted and not step_result.tool_calls:
            extracted = extract_tool_calls_from_text(
                step_result.answer or step_result.raw_answer,
                tool_defs,
                turn_index=cycle,
            )
            fallback_attempted = True
            if extracted:
                logger.info("Fallback extracted %d tool call(s)", len(extracted))
                step_result = LlmStepResult(
                    reasoning=step_result.reasoning,
                    answer=strip_textual_tool_calls(
                        step_result.answer, tool_defs
                    ),
                    tool_calls=extracted,
                    raw_answer=step_result.raw_answer,
                    finish_reason=step_result.finish_reason,
                )

        responses, tool_packets, citation_start = run_tool_calls(
            step_result.tool_calls or [],
            tools,
            citation_start=citation_start,
            max_concurrent_tools=app_config.MAX_CONCURRENT_TOOLS,
        )
        for p in tool_packets:
            yield p
        if responses:
            payloads = [r.llm_facing_response for r in responses]
            result.saw_tool_payload = True
            result.tool_urls |= collect_tool_urls(payloads)
            result.tool_report_ids |= collect_tool_report_ids(payloads)
            _append_tool_turn(history, responses, result)
            # resolve_items 3회차 차단: 후보 탐색은 2회(원문+핵심명사)면 충분.
            # 누적 2회 이후의 resolve는 소모성 반복이므로 스킵 통보.
            resolve_n = sum(1 for r in result.tool_calls
                            if r.tool_name == "mcp_jodal_resolve_items")
            if resolve_n >= 2:
                history.append(
                    ChatMessageSimple(
                        message="resolve_items는 2회로 충분하다. 더 호출하지 말고 가진 후보로 좁히거나 되물어라.",
                        message_type=MessageType.USER,
                    )
                )
            # Onyx select_reminder_text: after a web_search hit, nudge the
            # model toward open_url — gated on the tool actually existing.
            just_ran_web_search = any(
                r.tool_call is not None and r.tool_call.tool_name == "web_search"
                for r in responses
            )
            has_open_url = any(isinstance(t, OpenURLTool) for t in tools)
            if (
                just_ran_web_search
                and has_open_url
                and cycle < max_cycles - 1
            ):
                history.append(
                    ChatMessageSimple(
                        message=OPEN_URL_REMINDER,
                        message_type=MessageType.USER,
                    )
                )
        elif step_result.tool_calls:
            history.append(
                ChatMessageSimple(
                    message="All tool calls failed to dispatch. Answer directly.",
                    message_type=MessageType.USER,
                )
            )
            continue

        if step_result.answer and step_result.answer.strip():
            last_answer = step_result.answer
        pending_calls = bool(step_result.tool_calls)
        if not step_result.tool_calls:
            # 답 없이 '조회해볼게요'로 끝난 경우: 예고를 답으로 인정하지 않고
            # 재촉한다. 툴을 끝까지 안 부르면 (nudges 소진 후) 아래에서 강제 종료.
            # 툴을 이미 썼더라도 문장이 전부 예고면(_is_all_narration) 재촉한다 —
            # 그러면 사용자에게 실제 내용이 전혀 전달되지 않는다.
            nudgable = has_jodal_tools and promise_nudges < PROMISE_MAX_NUDGES
            no_tool_yet = not result.tool_calls
            if not nudgable or (
                not (_is_promise_only(step_result.answer) and no_tool_yet)
                and not _is_all_narration(step_result.answer)
            ):
                # Answer with no tool calls: done. Without this break the loop
                # re-sends the same history and streams a duplicate answer
                # every cycle (MAX_LLM_CYCLES repeats in one bubble).
                answer = step_result.answer
                break
            promise_nudges += 1
            logger.info(
                "Promise-only answer (nudge %d/%d): %s",
                promise_nudges, PROMISE_MAX_NUDGES, _trim(step_result.answer),
            )
            history.append(
                ChatMessageSimple(
                    message=TOOL_REQUIRED_REMINDER, message_type=MessageType.USER
                )
            )
            continue
    if not answer or not answer.strip():
        # Cycles exhausted with pending calls, or empty final text.
        answer = last_answer or _extractive_fallback(result)
    if answer:
        known = set(result.citation_docs.keys())
        if known:
            cleaned, removed = strip_unknown_citations(answer, known)
            if removed:
                logger.warning("Stripped unknown citation(s): %s", removed)
                answer = cleaned
        else:
            # 문서 citation이 하나도 없는데 본문에 [1]이 남으면 사용자는 존재하지
            # 않는 출처를 찾는다. Onyx 포트는 known이 비면 손대지 않지만, jodal 답은
            # citation이 별도 data-citation 채널로 오므로 본문 마커는 근거가 없다.
            # 연도·버전 표기([2024])는 floor 以上이라 건드리지 않는다.
            stripped, removed = strip_unknown_citations(
                answer, set(), strip_all_under_floor=True
            )
            if removed:
                logger.info("Stripped citation(s) with no documents: %s", removed)
                answer = stripped
    if answer and result.saw_tool_payload:
        # 사용자가 직접 붙인 URL은 지우면 안 된다.
        allowed = result.tool_urls | collect_tool_urls([latest_user_text])
        answer, dropped_urls = strip_fabricated_links(answer, allowed)
        if dropped_urls:
            logger.warning(
                "Dropped fabricated link(s) not produced by tools: %s", dropped_urls
            )
    if answer:
        if not user_mentions_report_id(latest_user_text):
            # 사용자가 ID를 물어본 게 아니면 툴이 준 ID라도 감춘다(내부 식별자).
            answer, hidden_ids = hide_report_ids(answer)
            if hidden_ids:
                logger.info("Hid report id(s) from answer: %s", hidden_ids)
        else:
            known_ids = result.tool_report_ids if result.saw_tool_payload else None
            answer, dropped_ids = strip_unknown_report_ids(answer, known_ids)
            if dropped_ids:
                logger.warning(
                    "Dropped unsupported report id(s): %s (tools ran=%s)",
                    dropped_ids, result.saw_tool_payload,
                )
        answer = strip_think_tags(answer)
    result.answer = (
        strip_trailing_narration(strip_leading_narration(answer))
        if answer
        else answer or ""
    )
    yield Packet(turn_index=cycle, type="stop")
    if pending_calls and not last_answer:
        logger.warning("Loop ended with pending tool calls; used fallback answer")
    return result


def _append_tool_turn(
    history: list[ChatMessageSimple],
    responses: list[ToolResponse],
    result: LoopResult,
) -> None:
    valid = [r for r in responses if r.tool_call is not None]
    simples = [
        ToolCallSimple(
            tool_call_id=r.tool_call.tool_call_id,  # type: ignore[union-attr]
            tool_name=r.tool_call.tool_name,  # type: ignore[union-attr]
            tool_arguments=r.tool_call.tool_args,  # type: ignore[union-attr]
        )
        for r in valid
    ]
    history.append(
        ChatMessageSimple(
            message="", message_type=MessageType.ASSISTANT, tool_calls=simples
        )
    )
    for r in valid:
        assert r.tool_call is not None
        history.append(
            ChatMessageSimple(
                message=r.llm_facing_response,
                message_type=MessageType.TOOL_CALL_RESPONSE,
                tool_call_id=r.tool_call.tool_call_id,
            )
        )
        docs: list[SearchDoc] | None = None
        rich = r.rich_response
        if isinstance(rich, dict) and isinstance(rich.get("search_docs"), list):
            docs = [SearchDoc(**d) for d in rich["search_docs"]]
            cmap = rich.get("citation_mapping") or {}
            nums = sorted(int(k) for k in cmap.keys()) if cmap else []
            for n, d in zip(nums, docs):
                result.citation_docs[n] = d
            if not nums:
                base = max(result.citation_docs.keys(), default=0)
                for i, d in enumerate(docs):
                    result.citation_docs[base + i + 1] = d
        result.tool_calls.append(
            ToolCallResult(
                tool_name=r.tool_call.tool_name,
                tool_arguments=r.tool_call.tool_args,
                tool_result=r.llm_facing_response,
                search_docs=docs,
            )
        )


def _extractive_fallback(result: LoopResult) -> str:
    """Last resort: summarize gathered search docs without another LLM call."""
    docs = [d for _, d in sorted(result.citation_docs.items())]
    if not docs:
        return "I couldn't produce an answer for that request."
    lines = ["Based on the search results:"]
    for i, doc in enumerate(docs[:6]):
        snippet = (doc.snippet or "")[:400].strip()
        lines.append(f"[{i + 1}] {doc.title} — {snippet}")
    return "\n".join(lines)
