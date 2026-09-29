#!/usr/bin/env python3
"""doc2query: 보고서별 '예상질문 + 값 별칭'을 LLM 으로 생성해 검색 문서를 보강한다.

de facto 표준 근거:
  - Doc2Query / docTTTTTquery (Nogueira & Lin 2019): 문서에서 예상 질의를 생성해
    인덱스에 병기하면 어휘 불일치(lexical gap)가 줄어든다. 예측-생성 계열의 원형.
  - InPars / Promptagator: LLM 으로 합성 질의를 만들어 검색기를 학습·보강.
  - RAGAS testset generation: 문서에서 질의-정답 쌍을 생성.

이 스크립트가 만드는 것 (프롬프트 미세조정이 아니라 **데이터 생성**):
  - queries: 사용자가 실제로 칠 법한 자연어 질문 4개 (보고서명 카피 금지, 구어체 포함)
  - aliases: 조건 선택값(기관명·시스템명)의 축약형/통칭 (예: 한국수자원공사 → 수자원공사)
  - synopsis: 어떤 질문에 답하는 보고서인지 1문장 (검색용 요약, 기존 데이터요약과 별개)

출력: data/catalog/doc2query.json  {report_id: {queries, aliases, synopsis, model, ts}}

재개 지원: 이미 생성된 report_id 는 건너뛴다 (LLM 크레딧 보호).

  uv run python pipeline/build_doc2query.py            # 전체 131
  uv run python pipeline/build_doc2query.py --limit 20 # 앞 20건만
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

API = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: F401,E402
import llm_chain  # noqa: E402

CATALOG = API / "data" / "catalog" / "report_catalog_w2d.json"
DETAILS = API / "data" / "details131.json"
FORM = API / "data" / "hubpick" / "form_guide.json"
OUT = API / "data" / "catalog" / "doc2query.json"

SYSTEM = (
    "너는 공공조달 데이터허브(보고서 131개) 검색을 돕는 인덱서다. "
    "사용자가 실제로 입력할 자연어 질문과 값 표기를 만들어 검색 인덱스에 붙일 데이터를 만든다. "
    "출력은 지정된 JSON 하나뿐이다."
)

PROMPT = """[보고서]
이름: {name}
설명: {desc}
차원(집계기준): {dims}
지표: {metrics}
조건(입력) 이름: {conds}
조건 선택값 일부: {opts}

[할 일]
1. queries: 사용자가 이 보고서를 찾을 때 실제로 칠 만한 자연어 질문 4개.
   - 보고서 이름을 그대로 복사하지 말 것 (사용자는 보고서명을 모른다)
   - 서로 다른 표현으로: 구어체 1개, 조건(기관·기간·유형) 포함 1개, 집계/통계 요구 1개, 업무 맥락 1개
   - 각 8~45자
2. aliases: 조건 선택값 중 사람이 줄여 부를 만한 것의 축약형 (없으면 빈 객체)
   예: {{"한국수자원공사": "수자원공사", "한국전력공사": "한전"}}
3. synopsis: 어떤 질문에 답하는지 1문장 (40자 내외)

JSON만 출력: {{"queries": ["...","...","...","..."], "aliases": {{}}, "synopsis": "..."}}"""


def load_inputs():
    cat = {r["report_id"]: r for r in json.loads(CATALOG.read_text(encoding="utf-8"))}
    det = json.loads(DETAILS.read_text(encoding="utf-8")) if DETAILS.exists() else {}
    form = json.loads(FORM.read_text(encoding="utf-8")) if FORM.exists() else {}
    return cat, det, form


def build_prompt(rid, cat, det, form):
    r = cat.get(rid) or {}
    d = det.get(rid) or {}
    desc = (d.get("desc") or r.get("desc_summary") or r.get("desc") or "")[:1200]
    conds = [c.get("name") or c.get("이름") for c in (r.get("입력조건_구조화") or [])]
    if not conds:
        conds = [c.get("name") for c in (form.get(rid) or [])]
    opts = []
    for c in (form.get(rid) or []):
        nm = c.get("name") or ""
        if "여부" in nm:
            continue
        for o in (c.get("opts") or [])[:12]:
            s = str(o).strip()
            if s and s not in opts:
                opts.append(s)
    return PROMPT.format(
        name=r.get("보고서명") or rid,
        desc=desc,
        dims=", ".join(r.get("dims") or []) or "없음",
        metrics=", ".join(r.get("metrics") or []) or "없음",
        conds=", ".join([str(c) for c in conds if c]) or "없음",
        opts=", ".join(opts[:30]) or "없음",
    )


def parse(text):
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    qs = [str(q).strip() for q in (d.get("queries") or []) if str(q).strip()]
    if not qs:
        return None
    al = {str(k).strip(): str(v).strip()
          for k, v in (d.get("aliases") or {}).items()
          if str(k).strip() and str(v).strip()}
    return {"queries": qs[:6], "aliases": al,
            "synopsis": str(d.get("synopsis") or "").strip()}


def main() -> None:
    ap = argparse.ArgumentParser(description="doc2query 생성 (LLM)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default=str(OUT))
    ap.add_argument("--temperature", type=float, default=0.7)
    a = ap.parse_args()

    cat, det, form = load_inputs()
    rids = sorted(cat)
    if a.limit:
        rids = rids[: a.limit]
    out_path = Path(a.out)
    store = json.loads(out_path.read_text(encoding="utf-8")) if out_path.exists() else {}
    todo = [r for r in rids if r not in store]
    print(f"[doc2query] 대상 {len(todo)}/{len(rids)} (기존 {len(rids)-len(todo)} 스킵)", flush=True)

    ok = fail = 0
    for i, rid in enumerate(todo, 1):
        prompt = build_prompt(rid, cat, det, form)
        try:
            text = llm_chain.chat([{"role": "system", "content": SYSTEM},
                                   {"role": "user", "content": prompt}],
                                  max_tokens=700, temperature=a.temperature)
            parsed = parse(text)
        except llm_chain.LLMExhausted as e:
            print(f"  [{rid}] LLMExhausted — 중단: {e}", flush=True)
            break
        if not parsed:
            fail += 1
            print(f"  [{rid}] 파싱 실패 — 스킵", flush=True)
            continue
        parsed["model"] = "xkiro"
        parsed["ts"] = datetime.now(timezone.utc).isoformat()
        store[rid] = parsed
        ok += 1
        out_path.write_text(json.dumps(store, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"  [{i}/{len(todo)}] {rid} q={len(parsed['queries'])} alias={len(parsed['aliases'])} "
              f"| {parsed['queries'][0][:34]}", flush=True)
    print(f"[doc2query] 완료 ok={ok} fail={fail} → {out_path}", flush=True)


if __name__ == "__main__":
    main()
