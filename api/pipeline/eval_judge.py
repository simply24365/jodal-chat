#!/usr/bin/env python3
"""LLM-as-judge 평가 체계 (설계 확정: MT-Bench / τ-bench / inspect_ai 리서치 근거).

3개 하위커맨드:

  run      자연 질의셋(data/eval/natural_queries.json)을 실행 서버에 던져
           runs/<label>.ndjson 을 만든다. 툴콜 로그(tool_name/arguments/result)와
           검색 상위 결과(retrieved_top)를 함께 기록한다.
             uv run python pipeline/eval_judge.py run --url http://localhost:8321 \
                 --out runs/head.ndjson --commit HEAD

  judge    NDJSON을 루브릭 5기준으로 채점한다. judge 모델은 xkiro 재사용
           (사용자 결정 — self-preference 편향은 리포트에서 명시한다).
             uv run python pipeline/eval_judge.py judge --in runs/head.ndjson

  compare  두 run을 같은 judge로 채점해 기준별 Δ, hit@1×coverage 교차표,
           회귀/개선 케이스를 evidence와 함께 출력한다.
             uv run python pipeline/eval_judge.py compare runs/head.ndjson runs/prev.ndjson

루브릭 (모두 높을수록 좋다):
  goldcoverage        binary. gold 보고서를 답이 실제 전달하면 1.
                      gold=[] (도메인밖)은 근거 없는 보고서 제시 없이
                      '없음/불가'로 정리하면 1.
  groundedness        binary. 답의 수치·ID·링크가 툴 결과에 근거하면 1.
  toolappropriateness 1-5.   필요한 조회를 적절한 툴로 수행했는지.
  hygieneresidue      binary. 1=잔여물 없음(미래형 약속 종료·나레이션·think 태그·
                      끊긴 링크 줄). 정규식 통과 케이스도 judge가 재검사한다.
  koreanquality       binary. 자연스러운 한국어면 1.

채점 규칙: 온도 0, JSON 강제 출력(response_format json_object),
파싱 실패 1회 재시도 → unscored. flagged(기준 간 모순) 케이스만 3회 다수결.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import os
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
EVAL_QUERIES = BASE_DIR / "data" / "eval" / "natural_queries.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: F401,E402  — api/.env 로딩 (llm_chain 전에)
import llm_chain  # noqa: E402

CRITERIA = ["goldcoverage", "groundedness", "toolappropriateness",
            "hygieneresidue", "koreanquality"]
BINARY = {"goldcoverage", "groundedness", "hygieneresidue", "koreanquality"}
TOOL_RESULT_MAX_CHARS = 1200
MAX_TOOL_CALLS = 6
NDJSON_TOP = 5  # retrieved_top 길이 (hit@1/hit@5 계산용)


def _read_ndjson(path: Path) -> list[dict]:
    # utf-8-sig: Windows 도구가 붙인 BOM 허용
    return [json.loads(x) for x in
            path.read_text(encoding="utf-8-sig").splitlines() if x.strip()]


# ---------------------------------------------------------------- run 모드

def _extract_report_ids(tool_result: str) -> list[str]:
    """툴 결과에서 report_id 후보를 등장 순서대로 뽑는다 (검색 candidates 우선)."""
    try:
        data = json.loads(tool_result)
        cands = data.get("candidates") if isinstance(data, dict) else None
        if isinstance(cands, list):
            ids = [c.get("report_id") for c in cands if isinstance(c, dict)]
            ids = [i for i in ids if isinstance(i, str)]
            if ids:
                return ids
    except Exception:
        pass
    return re.findall(r"\b\d{5}\b", tool_result)


def _channel_rich_ids(rec_tool_calls: list[dict]) -> list[str]:
    """채널 정직화: 검색 외 툴(value_lookup/get_report_detail 등)이 확인한 report_id.

    기관명 질의는 value_lookup(조건값→보고서 역색인) + get_report_detail 로
    정답을 확정하는 툴 체인이 정석 경로다. retrieved_top(검색 채널)만으로
    판정하면 이 정석 경로를 '미스'로 잘못 계산한다.
    """
    ids: list[str] = []
    for c in rec_tool_calls:
        name = c.get("tool_name") or ""
        if "value_lookup" in name or "get_report_detail" in name:
            for i in _extract_report_ids(c.get("tool_result") or ""):
                if i not in ids:
                    ids.append(i)
    return ids


def run_server(url: str, query: str, web_search: bool, timeout: int) -> dict:
    body = {"message": query, "stream": False, "web_search": web_search}
    req = urllib.request.Request(
        url.rstrip("/") + "/chat",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    return d, (time.monotonic() - t0) * 1000


def cmd_run(a: argparse.Namespace) -> None:
    all_queries = json.loads(EVAL_QUERIES.read_text(encoding="utf-8"))["queries"]
    # query_id 는 전체 질의셋 기준의 불변 위치 — 필터·재개와 무관하게 고정된다.
    pairs = [(f"n{i:02d}", item) for i, item in enumerate(all_queries, 1)]
    if a.limit:
        pairs = pairs[: a.limit]
    if a.skip_until:
        pairs = [p for p in pairs if p[0] >= a.skip_until]
    out = BASE_DIR / a.out
    out.parent.mkdir(parents=True, exist_ok=True)
    if a.append:
        done = {r["query_id"] for r in _read_ndjson(out)} if out.exists() else set()
        pairs = [p for p in pairs if p[0] not in done]
        if not pairs:
            print("[run] 재개할 미완료 질의가 없다 — 종료")
            return
    commit = a.commit
    if not commit:
        import subprocess
        commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                                cwd=BASE_DIR, capture_output=True,
                                text=True).stdout.strip() or "unknown"
    mode = "a" if a.append else "w"
    print(f"[run] {len(pairs)}개 질의 → {a.url} (commit={commit}, out={out}, mode={mode})")
    with out.open(mode, encoding="utf-8") as f:
        for qid, item in pairs:
            rec = {
                "query_id": qid,
                "query": item["q"],
                "commit": commit,
                "gold": item.get("gold") or [],
                "gold_any": item.get("gold_any"),
                "type": item.get("type", "?"),
                "final_answer": None,
                "tool_calls": [],
                "retrieved_top": [],
                "latency_ms": None,
                "error": None,
            }
            try:
                d, ms = run_server(a.url, item["q"], a.web_search, a.timeout)
                rec["latency_ms"] = round(ms)
                rec["final_answer"] = d.get("answer")
                calls = d.get("tool_calls") or []
                for c in calls[:MAX_TOOL_CALLS]:
                    rec["tool_calls"].append({
                        "tool_name": c.get("tool_name"),
                        "tool_arguments": c.get("tool_arguments"),
                        "tool_result": (c.get("tool_result") or "")[:TOOL_RESULT_MAX_CHARS],
                    })
                for c in calls:
                    rec["retrieved_top"] = _extract_report_ids(c.get("tool_result") or "")
                    if rec["retrieved_top"]:
                        break
                # 채널 정직화: value_lookup/get_report_detail 이 확정한 보고서를
                # 앞에 붙인다 (hit@k 계산에서 검색 채널과 동등하게 인정).
                # 검색 결과를 버리지 않고 뒤에 이어 붙여 NDJSON_TOP 개를 유지한다.
                rich = _channel_rich_ids(calls)
                merged = rich + [i for i in rec["retrieved_top"] if i not in rich]
                rec["retrieved_top"] = merged[:NDJSON_TOP]
            except Exception as e:  # 서버 다운·타임아웃도 레코드로 남긴다
                rec["error"] = str(e)[:200]
                print(f"  [{rec['query_id']}] ERROR {rec['error']}")
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            ans = (rec["final_answer"] or "")[:40].replace("\n", " ")
            print(f"  [{rec['query_id']}] {rec['latency_ms']}ms tool={len(rec['tool_calls'])} "
                  f"top={rec['retrieved_top'][:2]} ans={ans!r}")
    print(f"[run] 완료 → {out}")


# ---------------------------------------------------------------- judge 모드

JUDGE_SYSTEM = (
    "너는 한국어 검색-챗봇(RAG) 답변 품질 심사관이다. 제시된 근거만 보고 엄격하게 판정한다. "
    "출력은 요구된 JSON 하나뿐이다."
)

# 압축 루브릭: MT-Bench 계열 pairwise 루브릭을 pointwise로 줄이고,
# 각 기준을 한 줄로 한정해 judge 입력 토큰을 최소화한다 (항목당 설명 제거).
JUDGE_PROMPT = """[질의] {query}
[gold] {gold}  | 조건: {gold_any} | 유형: {qtype}
(gold 비어있음 = 도메인밖 질문 → 근거 없이 거절하면 정답)

[툴 기록]
{tool_calls}

[검색 top] {retrieved_top}

[답변]
{answer}

[확정된 기준 — 출력에서 제외] {fixed}

[채점 — 값이 클수록 좋음]
{fixed_note}
- groundedness 0/1: 답변의 수치·ID·링크가 툴 기록에 근거하면 1, 만들어냈으면 0.
- toolappropriateness 1-5: 5=정확한 툴·인자, 3=불필요/중복 포함, 1=조회 없이 추측.
- hygieneresidue 0/1: 1=잔여물 없음. 0=미래형 약속 종료("조회해드릴게요")·나레이션 잔여·think 태그·끊긴 링크 줄.
- koreanquality 0/1: 1=자연스러운 한국어.

JSON 하나만: {{"groundedness":0,"toolappropriateness":3,"hygieneresidue":1,"koreanquality":1,"goldcoverage":0,"evidence":"근거 1문장"}}"""


# ---------------------------------------------------- 프리필터 (LLM 토큰 절약)
# 리서치 근거: 결정적으로 판정 가능한 기준은 모델에 묻지 않는 것이 가장 싸다.
#   ("정규식 통과 케이스 재검사" = 발각 시에만 확정, 미발각은 모델에 위임)
_RESIDUE_PROMISE = re.compile(
    r"(?:조회|검색|확인|정리|살펴|안내|찾|모아|나열|작성|제시|전달|드릴|보여|알려|답변)"
    r"[^.!?\n]{0,10}?(?:해 ?드릴게요|해 ?줄게요|해 ?볼게요|해 ?보겠|하겠|드리겠|볼게요|줄게요)"
)
_RESIDUE_THINK = re.compile(r"<\s*/?\s*(?:think|thinking|thought|reason)\s*>", re.IGNORECASE)
_RESIDUE_LINK_LINE = re.compile(
    r"^[ \t]*(?:[-*+]\s*|\d+[.)]\s*)?(?:바로 ?열기|바로 ?보기|여기|링크|자세히 ?보기|접속|이동|열기|보기)[ \t]*:?[ \t]*$",
    re.MULTILINE,
)
_ID_RE = re.compile(r"(?<!\d)(\d{5})(?!\d)")
_URL_RE = re.compile(r"https?://[^\s)\]\"'<>\\]+")


def _prefilter(rec: dict) -> dict:
    """결정적(무료) 판정으로 확정할 수 있는 기준을 골라낸다.

    확정된 기준은 프롬프트의 [확정된 기준]에 적고 judge 출력에서 제외한다
    → 그 기준만큼 판단·출력 토큰이 줄어든다.
    """
    fixed: dict[str, int] = {}
    answer = rec.get("final_answer") or ""
    payload = "\n".join((c.get("tool_result") or "") for c in (rec.get("tool_calls") or []))

    # hygieneresidue: 잔여물이 "발견된" 경우만 0으로 확정 (미발각은 judge가 재검사)
    if _RESIDUE_THINK.search(answer) or _RESIDUE_LINK_LINE.search(answer):
        fixed["hygieneresidue"] = 0
    elif _RESIDUE_PROMISE.search(answer.split(".")[-1] if answer else ""):
        fixed["hygieneresidue"] = 0

    # groundedness: 답변에 있으나 툴 결과에 전혀 없는 5자리 ID·URL → 날조 확정
    if answer and payload:
        fake_ids = [i for i in set(_ID_RE.findall(answer)) if i not in payload]
        fake_urls = [u for u in set(_URL_RE.findall(answer)) if u.rstrip(".,);]\\") not in payload]
        if fake_ids or fake_urls:
            fixed["groundedness"] = 0

    return fixed


def _fixed_block(fixed: dict) -> tuple[str, str]:
    if not fixed:
        return "(없음)", "- goldcoverage 0/1: gold가 있으면 답변이 그 보고서를 실제 전달하면 1; gold 비어있으면 근거 없이 거절하면 1; 조건은 만족 여부로 판정."
    lines = "\n".join(f"- {k} = {v} (확정)" for k, v in fixed.items())
    remain = [c for c in ("goldcoverage", "groundedness") if c not in fixed]
    note = ""
    if "goldcoverage" in remain:
        note += "- goldcoverage 0/1: gold가 있으면 그 보고서를 실제 전달하면 1; gold 비어있으면 근거 없이 거절하면 1; 조건은 만족 여부로 판정.\n"
    if "groundedness" in remain:
        note += "- (groundedness는 위 채점 목록 참조.)\n"
    return lines, note


def _fmt_tool_calls(calls: list[dict]) -> str:
    if not calls:
        return "(툴 호출 없음)"
    lines = []
    for c in calls:
        args = json.dumps(c.get("tool_arguments"), ensure_ascii=False)
        lines.append(f"- {c.get('tool_name')} args={args}\n  result={c.get('tool_result')}")
    return "\n".join(lines)


def _build_judge_messages(rec: dict, pre: dict | None = None) -> list[dict]:
    fixed = pre if pre is not None else _prefilter(rec)
    fixed_lines, fixed_note = _fixed_block(fixed)
    user = JUDGE_PROMPT.format(
        query=rec["query"],
        gold=json.dumps(rec.get("gold") or [], ensure_ascii=False),
        gold_any=json.dumps(rec.get("gold_any"), ensure_ascii=False),
        qtype=rec.get("type", "?"),
        tool_calls=_fmt_tool_calls(rec.get("tool_calls") or []),
        retrieved_top=", ".join(rec.get("retrieved_top") or []) or "(없음)",
        answer=(rec.get("final_answer") or "(빈 답변)")[:4000],
        fixed=fixed_lines,
        fixed_note=fixed_note,
    )
    return [{"role": "system", "content": JUDGE_SYSTEM},
            {"role": "user", "content": user}]


def _parse_scores(text: str, pre: dict | None = None) -> dict | None:
    pre = pre or {}
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    need = [c for c in CRITERIA if c not in pre]  # 프리필터 확정분은 요구하지 않는다
    for k in need:
        v = d.get(k)
        if not isinstance(v, int):
            return None
        if k in BINARY and v not in (0, 1):
            return None
        if k == "toolappropriateness" and not 1 <= v <= 5:
            return None
    if not isinstance(d.get("evidence"), str) or not d["evidence"].strip():
        d["evidence"] = ""
    for k, v in pre.items():  # 확정분 병합
        d[k] = v
    return d


def _is_flagged(s: dict) -> bool:
    """기준 간 모순 → 다수결 재검사 대상."""
    if s["goldcoverage"] == 1 and s["groundedness"] == 0:
        return True
    if s["goldcoverage"] == 1 and s["toolappropriateness"] <= 2:
        return True
    if s["goldcoverage"] == 1 and s["hygieneresidue"] == 0:
        return True
    return False


def _judge_once(rec: dict, verbose: bool = False, pre: dict | None = None) -> dict | None:
    msgs = _build_judge_messages(rec, pre=pre)
    for attempt in (1, 2):  # 파싱 실패 1회 재시도 → unscored
        try:
            text = llm_chain.chat(msgs, max_tokens=400, temperature=0,
                                  verbose=verbose, json_mode=True)
        except llm_chain.LLMExhausted as e:
            print(f"  [judge] LLMExhausted: {e}")
            return None
        scores = _parse_scores(text, pre=pre)
        if scores is not None:
            return scores
        print(f"  [judge] 파싱 실패 (시도 {attempt}) — {text[:120]!r}")
    return None


def _judge_record(rec: dict, pre: dict | None = None, votes_target: int = 1) -> dict:
    """단일 레코드 채점.

    votes_target: 다수결 투표 수 (1=단일 판정, 3=self-consistency).
    temperature 0 이라도 free-tier 모델은 런 간 편차가 크므로(측정 ±0.14),
    중요 비교에서는 votes_target=3 로 안정화한다 — 토큰 절약 프리필터와 결합해
    추가 비용을 흡수한다.
    """
    pre = pre if pre is not None else _prefilter(rec)
    votes = []
    s = _judge_once(rec, pre=pre)
    if s is not None:
        votes.append(s)
        flagged = _is_flagged(s)
        if votes_target > 1 or flagged:
            print(f"  [judge] {rec['query_id']} 추가 투표 (flagged={flagged}, target={votes_target})")
            extra = [x for x in (_judge_once(rec, pre=pre)
                                 for _ in range(votes_target - 1)) if x is not None]
            votes.extend(extra)
    if not votes:
        return {"query_id": rec["query_id"], "scores": None, "votes": [],
                "flagged": False, "evidence": "unscored"}
    by_crit: dict[str, list[int]] = {k: [v[k] for v in votes] for k in CRITERIA}
    final = {k: Counter(v).most_common(1)[0][0] for k, v in by_crit.items()}
    evidence = next((v["evidence"] for v in votes
                     if all(v[k] == final[k] for k in CRITERIA)), votes[0]["evidence"])
    return {
        "query_id": rec["query_id"],
        "scores": final,
        "votes": [{k: v[k] for k in CRITERIA} for v in votes],
        "flagged": len(votes) > 1,
        "prefilter": pre,  # 프리필터로 확정한 기준 (LLM 토큰 절약 — 이 기준은 모델이 채점하지 않았다)
        "evidence": evidence,
    }


def _judge_model_name() -> str:
    provs = llm_chain._providers()
    return ", ".join(f"{p['name']}:{p['model']}" for p in provs[:2]) or "none"


def _rubric_version() -> str:
    """루브릭 프롬프트의 지문 해시 — 비교 가능한 채점 결과인지 판별하는 열쇠.

    실측: 동일 루브릭이면 온도0 xkiro 는 3회 독립 채점에서 stdev=0 (완전 결정적).
    즉 채점 총점이 달라지면 모델 비결정성이 아니라 '루브릭이 바뀐 것'이므로,
    버전 해시를 기록해 이종 루브릭 간 compare 를 차단한다.
    """
    blob = JUDGE_SYSTEM + JUDGE_PROMPT
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:10]


def cmd_judge(a: argparse.Namespace) -> None:
    src = BASE_DIR / a.input
    recs = _read_ndjson(src)
    if a.limit:
        recs = recs[: a.limit]
    out = BASE_DIR / a.output if a.output else src.with_suffix(".judged.json")
    print(f"[judge] {len(recs)}개 레코드 채점 — judge={_judge_model_name()}")
    judged = []
    for rec in recs:
        if rec.get("error") and not rec.get("final_answer"):
            print(f"  [{rec['query_id']}] 실행 실패 레코드 → unscored")
            judged.append({"query_id": rec["query_id"], "scores": None,
                           "votes": [], "flagged": False,
                           "evidence": f"run error: {rec['error']}"})
            continue
        print(f"  [{rec['query_id']}] 채점 중…")
        judged.append(_judge_record(rec, votes_target=a.votes))
    summary = _summarize(recs, judged)
    result = {
        "meta": {
            "source": str(src), "judge_model": _judge_model_name(),
            "rubric": {k: ("binary" if k in BINARY else "1-5") for k in CRITERIA},
            "rubric_version": _rubric_version(),
            "votes_target": a.votes,
            "note": "judge=xkiro 재사용 (self-preference 편향 감수)",
            "ts": datetime.now(timezone.utc).isoformat(),
        },
        "summary": summary,
        "records": judged,
    }
    out.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[judge] 완료 → {out}")
    print(json.dumps(summary, ensure_ascii=False, indent=1))


def _summarize(recs: list[dict], judged: list[dict]) -> dict:
    by_id = {r["query_id"]: r for r in recs}
    summary: dict = {"n": len(recs), "unscored": 0, "flagged": 0}
    for c in CRITERIA:
        summary[c] = None
    hits1 = hits5 = n_gold = 0
    crosstab = Counter()
    for j in judged:
        rec = by_id.get(j["query_id"], {})
        if j["scores"] is None:
            summary["unscored"] += 1
            continue
        if j["flagged"]:
            summary["flagged"] += 1
        for c in CRITERIA:
            norm = j["scores"][c] / 5 if c == "toolappropriateness" else j["scores"][c]
            summary[c] = (summary[c] or 0) + norm
        gold = rec.get("gold") or []
        if gold:
            n_gold += 1
            top = rec.get("retrieved_top") or []
            h1 = bool(set(top[:1]) & set(gold))
            h5 = bool(set(top[:NDJSON_TOP]) & set(gold))
            hits1 += h1
            hits5 += h5
            cov = j["scores"]["goldcoverage"]
            key = f"hit1={int(h1)},cov={cov}"
            crosstab[key] += 1
        elif rec.get("gold_any"):
            pass  # gold_any는 교차표에서 제외 (조건 판정은 judge 몫)
    if len(judged) > summary["unscored"]:
        for c in CRITERIA:
            if summary[c] is not None:
                summary[c] = round(summary[c] / (len(judged) - summary["unscored"]), 3)
        summary["total"] = round(sum(summary[c] or 0 for c in CRITERIA) / len(CRITERIA), 3)
    summary["hit@1"] = round(hits1 / n_gold, 3) if n_gold else None
    summary["hit@5"] = round(hits5 / n_gold, 3) if n_gold else None
    summary["crosstab_hit1_x_coverage"] = dict(crosstab)
    return summary


# ---------------------------------------------------------------- compare 모드

def _ensure_judged(path: Path) -> dict:
    judged_path = path.with_suffix(".judged.json")
    if judged_path.exists():
        print(f"[compare] 기존 채점 재사용: {judged_path}")
        return json.loads(judged_path.read_text(encoding="utf-8"))
    print(f"[compare] 채점 수행: {path} — judge={_judge_model_name()}")
    recs = _read_ndjson(path)
    judged = []
    for rec in recs:
        if rec.get("error") and not rec.get("final_answer"):
            judged.append({"query_id": rec["query_id"], "scores": None, "votes": [],
                           "flagged": False, "evidence": f"run error: {rec['error']}"})
            continue
        print(f"  [{rec['query_id']}] 채점 중…")
        judged.append(_judge_record(rec))
    result = {
        "meta": {"source": str(path), "judge_model": _judge_model_name(),
                 "rubric_version": _rubric_version(),
                 "note": "judge=xkiro 재사용 (self-preference 편향 감수)",
                 "ts": datetime.now(timezone.utc).isoformat()},
        "summary": _summarize(recs, judged),
        "records": judged,
    }
    judged_path.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[compare] 채점 저장 → {judged_path}")
    return result


def cmd_compare(a: argparse.Namespace) -> None:
    head_path, prev_path = BASE_DIR / a.head, BASE_DIR / a.prev
    head_rec = _read_ndjson(head_path)
    prev_rec = _read_ndjson(prev_path)
    head = _ensure_judged(head_path)
    prev = _ensure_judged(prev_path)
    if head["meta"]["judge_model"] != prev["meta"]["judge_model"]:
        print("[compare] 경고: 양쪽 judge 모델이 다르다 — judge 혼입 주의")
    hv = head["meta"].get("rubric_version")
    pv = prev["meta"].get("rubric_version")
    if hv != pv:
        print(f"[compare] 차단: 루브릭 버전이 다르다 ({pv} vs {hv}) — 채점 총점 비교 무효.")
        print("        양쪽 모두 현재 루브릭으로 재채점하라 (기존 judged.json 삭제 후 compare 재실행).")
        return

    # 기준별 Δ (같은 query_id끼리)
    h_scores = {j["query_id"]: j["scores"] for j in head["records"] if j["scores"]}
    p_scores = {j["query_id"]: j["scores"] for j in prev["records"] if j["scores"]}
    common = sorted(set(h_scores) & set(p_scores))
    delta = {}
    for c in CRITERIA:
        def norm(s):
            return s[c] / 5 if c == "toolappropriateness" else s[c]
        dh = sum(norm(h_scores[q]) for q in common) / len(common) if common else 0
        dp = sum(norm(p_scores[q]) for q in common) / len(common) if common else 0
        delta[c] = round(dh - dp, 3)
    total_h = head["summary"].get("total")
    total_p = prev["summary"].get("total")
    total_d = round((total_h or 0) - (total_p or 0), 3)
    subtle = abs(total_d) <= 0.03

    reg, imp = [], []
    for q in common:
        h, p = h_scores[q], p_scores[q]
        for c in CRITERIA:
            nh = h[c] / 5 if c == "toolappropriateness" else h[c]
            np_ = p[c] / 5 if c == "toolappropriateness" else p[c]
            if nh < np_:
                reg.append((q, c, p[c], h[c]))
            elif nh > np_:
                imp.append((q, c, p[c], h[c]))

    def _evidence_of(jrecs, q):
        for j in jrecs:
            if j["query_id"] == q:
                return j.get("evidence", "")
        return ""

    report = {
        "head": {"source": head["meta"]["source"], "summary": head["summary"]},
        "prev": {"source": prev["meta"]["source"], "summary": prev["summary"]},
        "n_common_scored": len(common),
        "delta_by_criterion": delta,
        "total": {"head": total_h, "prev": total_p, "delta": total_d,
                  "verdict": "유의 미묘 (±0.03 이내)" if subtle else "유의미한 변화"},
        "regressions": [
            {"query_id": q, "criterion": c, "prev": pv, "head": hv,
             "evidence": _evidence_of(head["records"], q)}
            for q, c, pv, hv in reg],
        "improvements": [
            {"query_id": q, "criterion": c, "prev": pv, "head": hv,
             "evidence": _evidence_of(head["records"], q)}
            for q, c, pv, hv in imp],
        "flagged_recheck": [
            {"query_id": j["query_id"], "evidence": j.get("evidence", "")}
            for j in head["records"] + prev["records"] if j.get("flagged")],
        "note": "judge는 HEAD 버전 하나만 사용해 양쪽 채점 (judge 혼입 방지)",
    }
    out = BASE_DIR / a.output if a.output else head_path.with_suffix(".compare.json")
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"[compare] 완료 → {out}")
    print(json.dumps({k: report[k] for k in
                      ("delta_by_criterion", "total", "n_common_scored")},
                     ensure_ascii=False, indent=1))
    if subtle and report["flagged_recheck"]:
        print(f"[compare] 총점 Δ가 미묘하므로 flagged {len(report['flagged_recheck'])}건 다수결 결과를 재확인하라.")


def main() -> None:
    ap = argparse.ArgumentParser(description="LLM-as-judge 평가 체계")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="자연 질의셋 실행 → NDJSON")
    r.add_argument("--url", default="http://localhost:8321")
    r.add_argument("--out", default="runs/head.ndjson")
    r.add_argument("--append", action="store_true", help="기존 파일에 이어서 기록 (미완료 run 재개)")
    r.add_argument("--skip-until", default=None, metavar="QID", help="해당 query_id 부터 실행")
    r.add_argument("--commit", default=None, help="기본: 현재 git short hash")
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--timeout", type=int, default=240)
    r.add_argument("--web-search", action="store_true",
                   help="웹검색 툴 포함 (기본: jodal 도메인 툴만)")

    j = sub.add_parser("judge", help="NDJSON 채점")
    j.add_argument("--input", "--in", dest="input", default="runs/head.ndjson")
    j.add_argument("--output", default=None)
    j.add_argument("--limit", type=int, default=0)
    j.add_argument("--votes", type=int, default=1,
                   help="레코드당 투표 수 (3=self-consistency 다수결, 편차 감소)")

    c = sub.add_parser("compare", help="두 run 비교 리포트")
    c.add_argument("head", help="runs/head.ndjson")
    c.add_argument("prev", help="runs/prev.ndjson")
    c.add_argument("--output", default=None)

    a = ap.parse_args()
    {"run": cmd_run, "judge": cmd_judge, "compare": cmd_compare}[a.cmd](a)


if __name__ == "__main__":
    main()
