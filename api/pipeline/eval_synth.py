#!/usr/bin/env python3
"""합성 질의셋 생성기 — 카탈로그 기반 다방면 시나리오 → LLM 질의 생성 → gold 검증.

리서치 근거 (RAGAS testset generation, HF eval cookbook, "50-150 Golden Queries"):
  - 문서(여기선 보고서 카탈로그)에서 질문-정답 쌍을 생성하면 볼륨이 빠르게 나온다.
  - 다양성은 시나리오 분포가 결정한다: 단일 엔티티 → 조건 결합 → 비교/메타 → 도메인밖.
  - LLM 합성 gold는 검증 없이 쓰면 오염된다 → 검색으로 2차 확인(hit 검증)을 거친다.

파이프라인:
  1. 카탈로그(131 보고서)에서 시나리오 슬롯을 채운다 (규칙: family/dims/is_visual 분포 반영).
  2. 각 시나리오를 LLM 프롬프트로 변환해 질의문을 생성한다 (json_mode, 온도 0.9).
  3. 생성된 질의를 실행 서버의 search_reports(또는 로컬 검색)로 되묻는다.
     gold로 지목된 보고서가 top-K에 재등장하지 않으면 오염 후보 → drop 또는 수동 검토.
  4. natural_queries.json 과 동일한 스키마로 저장 → eval_judge.py run 에 바로 투입.

사용:
  uv run python pipeline/eval_synth.py --n 30 --url http://localhost:8321
  uv run python pipeline/eval_synth.py --n 30 --no-server   # 검색 검증 생략 (빠른 초안)
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CATALOG = BASE_DIR / "data" / "catalog" / "report_catalog_w2d.json"
OUT_DEFAULT = BASE_DIR / "data" / "eval" / "synthetic_queries.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: F401,E402  — api/.env 로딩 (llm_chain 전에)
import llm_chain  # noqa: E402

# 시나리오 유형 × 비중 (RAGAS "evolution" 아이디어를 조달 도메인에 맞게 조정).
# 단순 조회는 소수, 조건 결합·비교·도메인밖을 넉넉히 — 어휘적 질의 편중을 막는다.
SCENARIO_MIX = [
    ("단일조회", 2),
    ("조건결합", 4),
    ("비교", 3),
    ("메타질문", 2),
    ("도메인밖", 2),
    ("구어체", 2),
]


def _catalog() -> list[dict]:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def _expand(mix: list[tuple[str, int]], total: int) -> list[str]:
    types = [t for t, w in mix for _ in range(w)]
    random.shuffle(types)
    return [types[i % len(types)] for i in range(total)]


def build_scenarios(n: int, seed: int) -> list[dict]:
    """시나리오 슬롯을 규칙으로 채운다. LLM은 질의문 다듬기만 담당 (환각 최소화)."""
    rows = _catalog()
    rng = random.Random(seed)
    types = _expand(SCENARIO_MIX, n)
    scenarios = []
    for i, t in enumerate(types):
        r = rng.choice(rows)
        sc = {
            "id": f"s{i:02d}",
            "type": t,
            "report": {
                "report_id": r["report_id"],
                "보고서명": r.get("보고서명"),
                "family": r.get("family"),
                "dims": r.get("dims") or [],
                "is_visual": bool(r.get("is_visual")),
            },
        }
        if t == "조건결합" and len(r.get("dims") or []) >= 1:
            sc["조건차원"] = rng.sample(r["dims"], min(2, len(r["dims"])))
        if t == "단일조회":
            sc["gold_hint"] = [r["report_id"]]
        scenarios.append(sc)
    return scenarios


SYNTH_SYSTEM = (
    "너는 조달데이터허브(공공 조달 통계 보고서 131개) 검색 챗봇의 평가 질의를 만드는 전문가다. "
    "실제 사용자가 챗에 입력할 법한 자연스러운 한국어 질문을 만든다. "
    "출력은 지정된 JSON 하나뿐이다."
)

SYNTH_PROMPT = """시나리오:
- 유형: {stype}
- 목표 보고서: {rname} (report_id {rid}, 분류 {family}, 차원 {dims}, 시각화 {visual})
{extra}

요구사항:
1. 위 보고서를 찾게 만드는 {stype} 스타일의 실사용자 질문 1개를 한국어로 작성한다.
2. 보고서명을 그대로 복사하지 말고 사람 말투로 바꿔라 (어휘적 질의 방지).
3. 유형별 규칙:
   - 단일조회: 특정 보고서/통계를 직접 요구
   - 조건결합: 조건차원({cdims})을 자연스럽게 문장에 녹인다
   - 비교: 두 지역/기업유형/기간 등을 견주는 표현
   - 메타질문: "몇 개나 있는지", "어떤 보고서가 있는지" 처럼 목록/집계를 묻는다
   - 구어체: "궁금해", "좀 알려줘" 같은 반말/구어 포함
   - 도메인밖: 조달 범위 밖 질문 (예: 날씨, 요리, 절차안내) — 보고서가 끌리지 않게
4. 질문 길이 10~60자. 예시 질문을 복사하지 말고 새로 쓴다.

출력 JSON: {{"question": "...", "reason": "이 질문이 시나리오를 만족하는 이유 1문장"}}"""


def _extra(sc: dict) -> str:
    if sc["type"] == "도메인밖":
        return "- 주의: 이 질문은 조달 도메인에 어떤 보고서도 매칭되면 안 된다."
    if sc["type"] == "메타질문":
        return "- 주의: gold는 조건 형태(gold_any)로 판정한다."
    return f"- gold_hint: {sc.get('gold_hint') or [sc['report']['report_id']]}"


def gen_questions(scenarios: list[dict], temperature: float = 0.9) -> list[dict]:
    out = []
    for sc in scenarios:
        r = sc["report"]
        msgs = [
            {"role": "system", "content": SYNTH_SYSTEM},
            {"role": "user", "content": SYNTH_PROMPT.format(
                stype=sc["type"], rname=r["보고서명"], rid=r["report_id"],
                family=r.get("family") or "-", dims=", ".join(r.get("dims") or []) or "없음",
                visual="예" if r.get("is_visual") else "아니오",
                extra=_extra(sc),
                cdims=", ".join(sc.get("조건차원") or []) or "없음",
            )},
        ]
        try:
            text = llm_chain.chat(msgs, max_tokens=300, temperature=temperature, json_mode=True)
        except llm_chain.LLMExhausted as e:
            print(f"  [{sc['id']}] LLM 실패 → 스킵: {e}")
            continue
        m = re.search(r"\{.*\}", text, re.DOTALL)
        try:
            d = json.loads(m.group(0)) if m else {}
        except json.JSONDecodeError:
            d = {}
        q = (d.get("question") or "").strip()
        if not q:
            print(f"  [{sc['id']}] 질의 파싱 실패 → 스킵: {text[:80]!r}")
            continue
        sc["question"] = q
        sc["gen_reason"] = (d.get("reason") or "").strip()
        out.append(sc)
        print(f"  [{sc['id']}] {sc['type']}: {q}")
    return out


# ------------------------------------------------------- gold 검증 (되묻기)

def _search(url: str, query: str, top_k: int = 5) -> list[str]:
    """실행 서버의 /mcp search_reports 로 되묻는다 (도메인밖은 호출하지 않는다)."""
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "search_reports",
                       "arguments": {"query": query, "top_k": top_k}}}
    # /mcp/ 트레일링 슬래시 필수 (리다이렉트 시 urllib이 POST를 유지하지 못한다).
    # Streamable HTTP 는 Accept 헤더에 application/json 명시를 요구한다.
    req = urllib.request.Request(
        url.rstrip("/") + "/mcp/", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read().decode())
    if d.get("error"):
        raise RuntimeError(f"MCP error: {d['error']}")
    payload = json.loads(d["result"]["content"][0]["text"])
    return [c["report_id"] for c in payload.get("candidates", []) if not c.get("dropped")]


def verify_gold(items: list[dict], url: str | None) -> list[dict]:
    """gold_hint로 지목된 보고서가 실제 검색 top-K에 재등장하는지 2차 확인.

    RAGAS 계열의 "합성 gold 오염 방지": LLM이 지목한 gold를 검색기로 역검증해
    재등장하지 않으면 신뢰 불가 → verified=false 표시 (자동 drop은 하지 않는다 —
    검색기 병목과 gold 오염을 구분해야 하므로).
    """
    for it in items:
        it["verified"] = None
        if it["type"] == "도메인밖":
            it["gold"] = []
            it["gold_any"] = None
            it["verified"] = "n/a"
            continue
        hint = it.get("gold_hint") or [it["report"]["report_id"]]
        it["gold"] = hint
        it["gold_any"] = None
        if it["type"] == "메타질문":
            it["gold"] = []
            it["gold_any"] = {"is_visual": it["report"]["is_visual"]} \
                if it["report"]["is_visual"] else {"dims_contains": (it["report"]["dims"] or ["-"])[0]}
        if url:
            try:
                top = _search(url, it["question"])
                it["retrieved_check"] = top
                it["verified"] = any(h in top for h in hint)
            except Exception as e:
                it["verified"] = None
                it["verify_error"] = str(e)[:120]
        ok = {None: "?", "n/a": "-", True: "OK", False: "X"}[it["verified"]]
        print(f"  [{it['id']}] gold={it['gold']} 검증={ok}")
    return items


def main() -> None:
    ap = argparse.ArgumentParser(description="합성 질의셋 생성")
    ap.add_argument("--n", type=int, default=30)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--url", default="http://localhost:8321",
                    help="gold 역검증에 쓸 실행 서버. --no-server 면 생략")
    ap.add_argument("--no-server", action="store_true")
    ap.add_argument("--temperature", type=float, default=0.9)
    ap.add_argument("--out", default=str(OUT_DEFAULT))
    a = ap.parse_args()

    print(f"[synth] 시나리오 {a.n}개 생성 (seed={a.seed})")
    scenarios = build_scenarios(a.n, a.seed)
    print("[synth] LLM 질의 생성")
    items = gen_questions(scenarios, a.temperature)
    print(f"[synth] 생성 {len(items)}/{a.n} — gold 검증")
    url = None if a.no_server else a.url
    items = verify_gold(items, url)

    queries = [{
        "q": it["question"],
        "gold": it.get("gold") or [],
        "gold_any": it.get("gold_any"),
        "type": it["type"],
        "note": f"synth:{it['id']} verified={it['verified']} — {it.get('gen_reason', '')}",
    } for it in items]
    out = {"_comment": [
        "LLM 합성 질의셋 (eval_synth.py 생성).",
        f"seed={a.seed}, scenarios={SCENARIO_MIX}, 카탈로그=report_catalog_w2d.json",
        "verified: OK=gold가 검색 top5 재등장 / X=미등장(gold 오염 또는 검색 병목) / ?=검증 생략",
    ], "queries": queries}
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    n_ok = sum(1 for it in items if it.get("verified") is True)
    n_x = sum(1 for it in items if it.get("verified") is False)
    print(f"[synth] 완료 → {a.out}  (생성 {len(queries)} · 검증 OK {n_ok} · X {n_x} · 생략 {len(items)-n_ok-n_x})")


if __name__ == "__main__":
    main()
