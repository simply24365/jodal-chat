#!/usr/bin/env python3
"""W1: 보고서 Canonical Metadata 정규화 (원본 불변 — 읽기만 함).

입력(bronze, 수정 금지):
  - public/reports131.json      (131종 목록)
  - data/details131.json        (입력 프롬프트 정의, 키=hubReptNo)
  - data/columns/{rid}_columns.json (113종, 나머지는 결측)

출력:
  - data/catalog/report_catalog.json (131 레코드)

규칙:
  - 조인 키 = hubReptNo 5자리. 키 불일치/중복 시 에러로 중단(사람 확인).
  - columns 미수집 건은 columns_missing: true 로 명시.
  - 원본 값은 그대로 복사 Etsy. 추론·보강 없음 (그건 W2).
"""
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
REPORTS_PATH = BASE_DIR / "public" / "reports131.json"
DETAILS_PATH = BASE_DIR / "data" / "details131.json"
COLUMNS_DIR = BASE_DIR / "data" / "columns"
OUT_PATH = BASE_DIR / "data" / "catalog" / "report_catalog.json"


def main():
    reports = json.loads(REPORTS_PATH.read_text(encoding="utf-8"))
    details = json.loads(DETAILS_PATH.read_text(encoding="utf-8"))

    # 키 무결성 검사
    rids = [str(r.get("hubReptNo(reptId)")).strip() for r in reports]
    if len(set(rids)) != len(rids):
        dupes = sorted({x for x in rids if rids.count(x) > 1})
        sys.exit(f"ABORT: reports131.json 중복 키 {dupes}")
    det_missing = [rid for rid in rids if rid not in details]
    if det_missing:
        sys.exit(f"ABORT: details131.json 에 없는 키 {det_missing}")

    catalog = []
    n_cols_ok = 0
    for r in reports:
        rid = str(r.get("hubReptNo(reptId)")).strip()
        det = details[rid]
        col_path = COLUMNS_DIR / f"{rid}_columns.json"
        if col_path.exists():
            cols = json.loads(col_path.read_text(encoding="utf-8"))
            columns_missing = False
            n_cols_ok += 1
        else:
            cols = None
            columns_missing = True

        prompts = det.get("prompts", []) or []
        catalog.append({
            "report_id": rid,
            "official_id": r.get("보고서ID"),
            "보고서명": r.get("보고서명"),
            "reptSubNm": r.get("reptSubNm"),
            "업무분류": None,  # W2에서 확정 (원천에 없음)
            "설명": r.get("설명"),
            "desc": det.get("desc"),
            "mstrFrmt": r.get("mstrFrmt"),
            "mstrReptIdVal": r.get("mstrReptIdVal"),
            "작성일자": r.get("작성일자"),
            "조회수": r.get("조회수"),
            "입력조건": [
                {"이름": p.get("title"), "UI타입": p.get("ui"),
                 "pin": p.get("pin"), "optsTotal": p.get("optsTotal"),
                 "opts": p.get("opts", [])}
                for p in prompts
            ],
            "columns_missing": columns_missing,
            "columns_source": (cols.get("source") or cols.get("export_path")) if cols else None,
            "total_columns": cols.get("total_columns") if cols else None,
            "total_data_rows": cols.get("total_data_rows") if cols else None,
            "metrics": (cols.get("metrics_summary") or {}).get("metrics") if cols else None,
            "dimensions": (cols.get("dimensions_summary") or {}).get("dimensions") if cols else None,
            "identifiers": (cols.get("identifiers_summary") or {}).get("identifiers") if cols else None,
            "dates": (cols.get("dates_summary") or {}).get("dates") if cols else None,
            "columns": cols.get("columns") if cols else None,
            "prompt_summary": cols.get("prompt_summary") if cols else None,
            "full_report_title": cols.get("full_report_title") if cols else None,
        })

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[W1] records={len(catalog)} columns_ok={n_cols_ok} "
          f"columns_missing={len(catalog) - n_cols_ok} -> {OUT_PATH}")
    # 원본 무결성: 입력 파일 mtime不问, 쓰기 없음 — 읽기만 수행 (검증용 assert)
    assert len(catalog) == 131, f"레코드 수 {len(catalog)} != 131"


if __name__ == "__main__":
    main()
