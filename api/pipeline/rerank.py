#!/usr/bin/env python3
"""W5 LLM Rerank + W6 No Match. 프롬프트 v1 고정, structured output 검증.

  from rerank import decide
  decide("서울 공공기관 노트북 계약금액?", top_k=10, final_n=3)
  -> {"no_match": False, "top": [{report_id, match, reason,
        missing_capabilities, confidence}], "verdicts": [...]}

CLI: uv run python tools/rerank.py "질문" [--top-k 10] [--final-n 3]
임계값: env NO_MATCH_CONF (기본 0.5)
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from search_hybrid import query as hybrid_query  # noqa: E402
from llm_chain import chat, LLMExhausted  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
CATALOG_PATH = BASE_DIR / "data" / "catalog" / "report_catalog_w2c.json"
THRESHOLD = float(os.environ.get("NO_MATCH_CONF", "0.5"))
NO_MATCH_MSG = "정확히 조건을 만족하는 보고서 없음"

PROMPT_V1 = """조달데이터허브 보고서 추천 판정. 사용자 질문에 각 후보 보고서가 적합한지 판단하라.

판정 기준:
- match=true: 질문의 지표(금액/건수 등)와 조건(기간/기관/지역/품목 등)을 해당 보고서의
  출력컬럼·입력조건으로 충족할 수 있을 때.
- missing_capabilities: 충족 못하는 요구를 짧게. 없으면 [].
- confidence: 0~1. 애매하면 낮게.
- reason은 반드시 한국어 10자 이상 근거 문장 (빈칸·한 단어 불가).
- 아래 FACT는 카탈로그 확정 사실이다. FACT와 모순되는 판정 금지
  (예: FACT에 없는 지표를 있다고 하지 말 것).
- 억지로 맞추지 말 것. 맞는 게 없으면 전부 match=false.

반드시 JSON 배열만 출력 (설명·코드펜스 금지):
[{"report_id":"00118","match":true,"reason":"근거 한 줄",
"missing_capabilities":[],"confidence":0.94}]
배열 길이는 반드시 {n}개, 아래 순서 그대로.

사용자 질문: {q}

후보:
{items}"""


def _catalog():
    if not hasattr(_catalog, "cache"):
        _catalog.cache = {r["report_id"]: r for r in
                          json.loads(CATALOG_PATH.read_text(encoding="utf-8"))}
    return _catalog.cache


def _ctx(rid):
    r = _catalog()[rid]
    conds = []
    groups = {"기간": [], "주체": [], "구분": [], "입력": []}
    for c in (r.get("입력조건_구조화") or [])[:25]:
        opts = c.get("허용값") or []
        o = f"={','.join(opts[:5])}" if opts else ""
        conds.append(f"{c['이름']}({c['의미종류']}{o})")
        sem = c.get("의미종류") or ""
        if sem in ("date", "yearmonth", "year"):
            groups["기간"].append(c["이름"])
        elif sem in ("entity", "entity_multi"):
            groups["주체"].append(c["이름"])
        elif sem.startswith("enum") or sem in ("radio", "selectbox"):
            groups["구분"].append(c["이름"])
        else:
            groups["입력"].append(c["이름"])
    fact = ("지표=[" + ", ".join(r.get("metrics") or []) + "]; " +
            "차원=[" + ", ".join(r.get("dimensions") or []) + "]; " +
            "; ".join(f"조건:{k}=[{', '.join(v)}]" for k, v in groups.items()))
    return (f"- {rid} {r.get('보고서명')}\n"
            f"  설명: {(r.get('desc_summary') or '')[:400]}\n"
            f"  조건: {'; '.join(conds)}\n"
            f"  지표: {', '.join(r.get('metrics') or [])}\n"
            f"  차원: {', '.join(r.get('dimensions') or [])}\n"
            f"  FACT(확정): {fact}")


def _parse(text, want):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip()).strip()
    m = re.search(r"\[.*\]", t, re.DOTALL)
    arr = json.loads(m.group(0)) if m else json.loads(t)
    assert isinstance(arr, list) and arr, "빈 배열"
    by_id = {}
    for a in arr:
        assert isinstance(a.get("match"), bool), f"match 불리언 아님: {a}"
        assert a.get("report_id") in want, f"후보 외 ID: {a.get('report_id')}"
        a["confidence"] = float(a.get("confidence", 0))
        rs = str(a.get("reason") or "").strip()
        assert len(rs) >= 10, f"reason 부실: {a.get('report_id')}"
        a["reason"] = rs
        a.setdefault("missing_capabilities", [])
        by_id[a["report_id"]] = a
    return by_id


def rerank(question, candidates):
    """candidates: [(rid, rrf, rb, rv)]. 반환: {rid: 판정} (실패 후보는 탈락=제외)."""
    want = [rid for rid, _, _, _ in candidates]
    prompt = PROMPT_V1.replace("{n}", str(len(want))).replace("{q}", question).replace(
        "{items}", "\n".join(_ctx(rid) for rid in want))
    last = ""
    for _ in range(2):
        try:
            text = chat([{"role": "user", "content": prompt}],
                        max_tokens=4096, temperature=0, json_mode=True)
        except LLMExhausted as e:
            last = f"LLM 소진: {e}"
            break
        try:
            return _parse(text, set(want)), None
        except Exception as e:
            last = f"검증 실패: {str(e)[:150]}"
            print(f"  {last}", flush=True)
    return {}, last


def decide(question, top_k=10, final_n=3, threshold=THRESHOLD, verbose=True):
    cands = hybrid_query(question, top_k=top_k)
    verdicts, err = rerank(question, cands)
    if err and not verdicts:
        return {"no_match": True, "message": NO_MATCH_MSG,
                "top": [], "verdicts": {}, "error": err,
                "candidates": [c[0] for c in cands]}
    matched = sorted((v for v in verdicts.values()
                      if v["match"] and v["confidence"] >= threshold),
                     key=lambda x: -x["confidence"])[:final_n]
    if not matched:
        near = sorted(verdicts.values(), key=lambda x: -x["confidence"])[:2]
        return {"no_match": True, "message": NO_MATCH_MSG,
                "top": [], "reference": [v["report_id"] for v in near],
                "verdicts": verdicts,
                "candidates": [c[0] for c in cands]}
    return {"no_match": False, "message": "",
            "top": matched, "verdicts": verdicts,
            "candidates": [c[0] for c in cands]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--final-n", type=int, default=3)
    a = ap.parse_args()
    res = decide(a.question, top_k=a.top_k, final_n=a.final_n)
    if res["no_match"]:
        print(NO_MATCH_MSG)
        if res.get("reference"):
            print("참고 후보:", res["reference"])
    else:
        for v in res["top"]:
            print(f"{v['report_id']} conf={v['confidence']:.2f} | {v['reason'][:120]}")
            if v["missing_capabilities"]:
                print(f"  미충족: {v['missing_capabilities']}")


if __name__ == "__main__":
    main()
