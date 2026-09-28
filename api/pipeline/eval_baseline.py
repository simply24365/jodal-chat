#!/usr/bin/env python3
"""P0: 베이스라인 측정 (검색 단계, LLM 0회, 결정적).

Gold 미니셋: 카탈로그에서 합성 (보고서당 2문항).
  Q1 쉬움: "{보고서명} 보여줘" (gold=rid)
  Q2 실전형: "{dims}별 {지표} 알려줘" (dims+metrics 보유 건만, gold=rid)
지표: recall@5, family_confusion(top1 family 불일치율),
     dims_miss(dims 명시 쿼리에서 top1 dims 불일치율).
출력: data/eval/baseline.json (재실행 비교용).
"""
import json
import random
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CATALOG_PATH = BASE_DIR / "data" / "catalog" / "report_catalog_w2d.json"
OUT_PATH = BASE_DIR / "data" / "eval" / "baseline.json"


def build_gold(catalog, seed=7, per_family=3):
    rnd = random.Random(seed)
    by_fam = {}
    for r in catalog:
        by_fam.setdefault(r["family"], []).append(r)
    gold = []
    for fam, rs in sorted(by_fam.items()):
        for r in rnd.sample(rs, min(per_family, len(rs))):
            gold.append({"q": f"{r['보고서명']} 보여줘", "gold": r["report_id"],
                         "family": fam, "dims": r["dims"], "kind": "easy"})
            mets = (r.get("metrics") or [])[:1]
            if r["dims"] and mets:
                gold.append({"q": f"{' '.join(r['dims'])}별 {mets[0]} 알려줘",
                             "gold": r["report_id"], "family": fam,
                             "dims": r["dims"], "kind": "dims_metric"})
    return gold


def main():
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from app.retrieval.search_hybrid import query
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    by_id = {r["report_id"]: r for r in catalog}
    gold = build_gold(catalog)
    print(f"[P0] 문항 {len(gold)}건 (easy/dims_metric)")

    rec5 = fam_miss = dims_q = dims_miss = 0
    for g in gold:
        res = query(g["q"], top_k=5)
        rids = [r[0] for r in res]
        if g["gold"] in rids:
            rec5 += 1
        top = by_id[rids[0]]
        if top["family"] != g["family"]:
            fam_miss += 1
        if g["kind"] == "dims_metric":
            dims_q += 1
            if not set(g["dims"]) & set(top["dims"]):
                dims_miss += 1

    n = len(gold)
    out = {
        "n": n,
        "recall@5": round(rec5 / n, 3),
        "family_confusion": round(fam_miss / n, 3),
        "dims_miss": round(dims_miss / dims_q, 3) if dims_q else None,
        "dims_questions": dims_q,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[P0] {out} -> {OUT_PATH}")


if __name__ == "__main__":
    main()
