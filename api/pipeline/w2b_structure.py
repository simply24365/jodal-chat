#!/usr/bin/env python3
"""W2b: 입력조건 구조화 (100% 결정적, LLM 불사용, 원본 불변).

입력(읽기만): data/catalog/report_catalog_w2a.json
출력: data/catalog/report_catalog_w2b.json (입력조건_구조화 + 조건요약 추가)

UI타입 → 의미종류 매핑:
  resfromdate/restodate/date agregator → date (from/to는 이름의 From/To로 판별)
  yymmfromdate/yymmtodate/yymmdate → yearmonth
  multiyear → year (opts=연도 목록)
  fromnum/tonum → number
  text → text (자유입력)
  popup/popups → entity (팝업검색, s 복수)
  selectbox/multiselectbox/radio → enum (opts=허용값)
필수여부: required_unknown (Alert 유발 실험 데이터 없으므로, 별도 과제).
"""
import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
IN_PATH = BASE_DIR / "data" / "catalog" / "report_catalog_w2a.json"
OUT_PATH = BASE_DIR / "data" / "catalog" / "report_catalog_w2b.json"

SEMANTIC = {
    "resfromdate": "date", "restodate": "date", "date": "date",
    "yymmfromdate": "yearmonth", "yymmtodate": "yearmonth", "yymmdate": "yearmonth",
    "multiyear": "year",
    "fromnum": "number", "tonum": "number",
    "text": "text",
    "popup": "entity", "popups": "entity_multi",
    "selectbox": "enum", "multiselectbox": "enum_multi", "radio": "enum",
}


def structure_one(p):
    ui = p.get("UI타입") or ""
    name = (p.get("이름") or "").strip()
    sem = SEMANTIC.get(ui, "unknown")
    bound = None
    if sem in ("date", "yearmonth", "number"):
        if re.search(r"from|\(from\)|부터|시작", name, re.I):
            bound = "from"
        elif re.search(r"to|\(to\)|까지|종료", name, re.I):
            bound = "to"
    base_name = re.sub(r"\s*[\(_\[](from|to|전일|년)[\)\]]?\s*$", "", name, flags=re.I).strip("_() ")
    return {
        "이름": name,
        "기본명": base_name or name,
        "UI타입": ui,
        "의미종류": sem,
        "범위": bound,                       # from/to/None
        "필수여부": "required_unknown",       # W2b 원칙: 실험 전까지 미확정
        "허용값": p.get("opts") or [],
        "허용값수": p.get("optsTotal") or len(p.get("opts") or []),
        "pin": p.get("pin"),
    }


def main():
    catalog = json.loads(IN_PATH.read_text(encoding="utf-8"))
    out = []
    unmapped = set()
    for r in catalog:
        conds = [structure_one(p) for p in (r.get("입력조건") or [])]
        for c in conds:
            if c["의미종류"] == "unknown":
                unmapped.add(c["UI타입"])
        rec = dict(r)
        rec["입력조건_구조화"] = conds
        rec["조건요약"] = {
            "총조건수": len(conds),
            "enum조건수": sum(1 for c in conds if c["의미종류"].startswith("enum")),
            "날짜조건수": sum(1 for c in conds if c["의미종류"] in ("date", "yearmonth", "year")),
            "자유입력수": sum(1 for c in conds if c["의미종류"] in ("text", "entity", "entity_multi", "number")),
        }
        out.append(rec)
    if unmapped:
        raise SystemExit(f"ABORT: 미매핑 UI타입 {sorted(unmapped)} — SEMANTIC에 추가 필요")
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    assert len(out) == 131
    from collections import Counter
    sem = Counter(c["의미종류"] for r in out for c in r["입력조건_구조화"])
    print(f"[W2b] records={len(out)} -> {OUT_PATH}")
    print("의미종류 분포:", dict(sem))


if __name__ == "__main__":
    main()
