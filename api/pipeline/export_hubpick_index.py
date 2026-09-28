#!/usr/bin/env python3
"""hubpick 에셋 빌드: mcp/public/hubpick/*.json (form/value/family/concept/health/names).

원본 불변. rerank_ctx.json과 무관한 별도 에셋 — 기존 6툴에 영향 없음.
실행: python3 knowledge/tools/export_hubpick_index.py (repo root 어디서든 OK)
"""
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve()
KNOW = HERE.parent.parent  # knowledge/
REPO = KNOW.parent  # jodal-chat/
W2D = KNOW / "data" / "catalog" / "report_catalog_w2d.json"
CV = KNOW / "data" / "catalog" / "col_values.json"
CONCEPTS = KNOW / "data" / "catalog" / "concepts.json"
SUMDIR = KNOW / "data" / "summaries"
OUT = REPO / "mcp" / "public" / "hubpick"
HUBPICK_VER = "w2d-20260919|hubpick-1"


def main():
    cat = json.loads(W2D.read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)

    names = {}
    forms = {}
    fam = {}
    for r in cat:
        rid = r["report_id"]
        names[rid] = r.get("보고서명") or rid
        conds = []
        for x in (r.get("입력조건_구조화") or []):
            conds.append({
                "name": x.get("이름"),
                "ui": x.get("UI타입"),
                "sem": x.get("의미종류"),
                "required": x.get("필수여부"),
                "opts": x.get("허용값") or [],
            })
        forms[rid] = conds
        f = r.get("family") or "미분류"
        dims = r.get("dims") or []
        vis = bool(r.get("is_visual"))
        fam.setdefault(f, []).append({
            "rid": rid,
            "name": names[rid],
            "official_id": r.get("official_id"),
            "dims": dims,
            "conds": [c["name"] for c in conds],
            "is_visual": vis,
            "kind": "시각화" if vis else ("내역형" if not dims else "통계형"),
            "family_conf": r.get("family_conf"),
        })

    (OUT / "names.json").write_text(json.dumps(names, ensure_ascii=False), encoding="utf-8")
    (OUT / "form_guide.json").write_text(json.dumps(forms, ensure_ascii=False), encoding="utf-8")
    (OUT / "family_index.json").write_text(
        json.dumps({"families": fam, "hubpick_ver": HUBPICK_VER}, ensure_ascii=False),
        encoding="utf-8")

    concepts = json.loads(CONCEPTS.read_text(encoding="utf-8"))
    (OUT / "concept_index.json").write_text(
        json.dumps(concepts, ensure_ascii=False), encoding="utf-8")

    cv = json.loads(CV.read_text(encoding="utf-8"))
    values = {}
    for rid, r in cv.items():
        cols = {}
        for col, v in (r.get("columns") or {}).items():
            cols[col] = {"n": v.get("n"), "distinct": v.get("distinct"),
                         "top": v.get("top") or []}
        values[rid] = cols
    (OUT / "value_topk.json").write_text(
        json.dumps(values, ensure_ascii=False), encoding="utf-8")

    w2c = Counter(x.get("w2c_status") for x in cat)
    fconf = Counter(x.get("family_conf") for x in cat)
    nodims = [x["report_id"] for x in cat if not x.get("dims")]
    nocol = [x["report_id"] for x in cat if x.get("w2c_status") == "no_columns"]
    have = {p.stem for p in SUMDIR.glob("*.txt")} if SUMDIR.is_dir() else set()
    missing = [x["report_id"] for x in cat if x["report_id"] not in have]
    health = {
        "hubpick_ver": HUBPICK_VER,
        "reports": len(cat),
        "w2c": {"applied": w2c.get("applied", 0), "no_columns": w2c.get("no_columns", 0),
                "no_columns_rids": nocol},
        "family_conf": {"rule": fconf.get("rule", 0), "manual": fconf.get("manual", 0)},
        "dims_missing_rids": nodims,
        "summaries_missing": missing,
    }
    (OUT / "health.json").write_text(json.dumps(health, ensure_ascii=False), encoding="utf-8")

    for f in sorted(OUT.glob("*.json")):
        print(f"{f.name}: {f.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
