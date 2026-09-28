#!/usr/bin/env python3
"""W3: 검색용 문서 생성 (100% 결정적, LLM 불사용, 원본 불변).

입력(읽기만): data/catalog/report_catalog_w2c.json (+ data/catalog/concepts.json 있으면 동의어 주입)
출력:
  - data/catalog/search_docs/{report_id}.txt (보고서별 6섹션 + 동의어)
  - data/catalog/search_docs.jsonl (일괄본: {report_id, text})

템플릿:
  보고서명 / 설명(요약, 원문 폴백) / 입력조건 / 출력컬럼 / 지표 / 차원 [+ 동의어]
규칙:
  - 격리(quarantine) 컬럼 제외, columns_missing 건은 해당 섹션에 '정보 없음' 명시.
  - 동의어: concepts.json 중 해당 보고서 컬럼과 매칭되는 개념만
    "label = alias1, alias2" + 정의(있으면) 형식으로.
  - 빈 문서·과단 문서(합 50자 미만)는 assert 실패.
"""
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
IN_PATH = BASE_DIR / "data" / "catalog" / "report_catalog_w2c.json"
CONCEPTS_PATH = BASE_DIR / "data" / "catalog" / "concepts.json"  # 없으면 동의어 스킵
COL_VALUES_PATH = BASE_DIR / "data" / "catalog" / "col_values.json"  # 없으면 값예시 스킵
DOCS_DIR = BASE_DIR / "data" / "catalog" / "search_docs"
JSONL_PATH = BASE_DIR / "data" / "catalog" / "search_docs.jsonl"


def matched_concepts(r, concepts):
    """해당 보고서의 비격리 컬럼명과 매칭되는 개념만 반환."""
    colnames = [c["name"] for c in (r.get("columns") or [])
                if not (c.get("w2c") or {}).get("quarantine")]
    out = []
    for c in concepts:
        terms = [c["label"]] + (c.get("aliases") or [])
        if any(t and any(t in n or n in t for n in colnames) for t in terms):
            out.append(c)
    return out


COL_VALUES: dict = {}
SUMM_DIR = BASE_DIR / "data" / "summaries"


def load_summary(rid):
    """LLM 생성 테이블 설명 로드. 없거나 비어있으면 None."""
    if not rid:
        return None
    p = SUMM_DIR / f"{rid}.txt"
    try:
        t = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return t or None
def render(r, concepts):
    L = [f"# {r.get('보고서명')}", ""]
    desc = r.get("desc_summary") or r.get("desc_clean") or r.get("설명") or ""
    L += ["## 설명", desc.strip(), ""]
    conds = r.get("입력조건_구조화") or []
    L.append("## 입력조건")
    if conds:
        for c in conds:
            extra = f" [{c['범위']}]" if c.get("범위") else ""
            opts = c.get("허용값") or []
            opt_s = f" = {', '.join(opts[:15])}" + ("…" if len(opts) > 15 else "") if opts else ""
            L.append(f"- {c['이름']} ({c['의미종류']}{extra}){opt_s}")
    else:
        L.append("(정보 없음)")
    L.append("")
    cols = [c for c in (r.get("columns") or []) if not (c.get("w2c") or {}).get("quarantine")]
    L.append("## 출력컬럼")
    L.append(", ".join(c["name"] for c in cols) if cols else "(정보 없음)")
    L.append("")
    for sec, key in (("지표", "metrics"), ("차원", "dimensions")):
        vals = r.get(key) or []
        L.append(f"## {sec}")
        L.append(", ".join(vals) if vals else "(정보 없음)")
        L.append("")
    # 데이터 요약: LLM이 쓴 테이블 설명 1~2문장 (data/summaries/{rid}.txt).
    # 없으면 섹션 생략. 값 나열(top30)은 토큰만 먹고 중위권 miss라 폐기.
    # 생성: tools/gen_summaries_agnes.py (Agnes 3.0 Flash, 131건).
    summ = load_summary(r.get("report_id"))
    if summ:
        L.append("## 데이터요약")
        L.append(summ)
        L.append("")
    syn = [f"{c['label']} = {', '.join(c.get('aliases') or [])}" +
           (f" ({c['definition']})" if c.get("definition") else "")
           for c in matched_concepts(r, concepts)]
    if syn:
        L += ["## 동의어"] + [f"- {s}" for s in syn] + [""]
    return "\n".join(L).strip() + "\n"


def main():
    global COL_VALUES
    catalog = json.loads(IN_PATH.read_text(encoding="utf-8"))
    concepts = []
    if CONCEPTS_PATH.exists():
        concepts = json.loads(CONCEPTS_PATH.read_text(encoding="utf-8")).get("concepts", [])
    if COL_VALUES_PATH.exists():
        COL_VALUES = json.loads(COL_VALUES_PATH.read_text(encoding="utf-8"))
    rows, shorts = [], []
    for r in catalog:
        text = render(r, concepts)
        assert len(text) >= 50, f"{r['report_id']}: 과단 문서 ({len(text)}자)"
        (DOCS_DIR / f"{r['report_id']}.txt").write_text(text, encoding="utf-8")
        rows.append({"report_id": r["report_id"], "text": text})
    JSONL_PATH.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in rows),
                          encoding="utf-8")
    avg = sum(len(x["text"]) for x in rows) // len(rows)
    n_syn = sum(1 for x in rows if "## 동의어" in x["text"])
    print(f"[W3] docs={len(rows)} avg_len={avg}자 concepts={len(concepts)} 동의어보유={n_syn}")
    print(f"  -> {DOCS_DIR}/ + {JSONL_PATH}")


if __name__ == "__main__":
    main()
