#!/usr/bin/env python3
"""Stage solver 측정 (gold 미니셋, LLM 0회). eval_baseline.py와 동일 문항.
family_confusion은 신호 있음(easy)/모호(dims_metric 무family) 분리 집계."""
import json
import sys
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))
from app.retrieval.slot_parser import parse
from app.retrieval.stage_solve import solve
from app.retrieval.search_hybrid import query as rrf_query
from eval_baseline import build_gold

catalog = json.loads((BASE_DIR / "data" / "catalog" / "report_catalog_w2d.json").read_text(encoding="utf-8"))
by_id = {r["report_id"]: r for r in catalog}
gold = build_gold(catalog)
rec5 = fam_sig = fam_amb = n_sig = n_amb = dims_q = dims_miss = 0
for g in gold:
    rq = rrf_query(g["q"], top_k=50)
    rr = {r[0]: r[1] for r in rq}
    b1 = next((r[0] for r in rq if r[2] == 1), None)
    v1 = next((r[0] for r in rq if r[3] == 1), None)
    consensus = b1 if b1 and b1 == v1 else None
    p = parse(g["q"])
    top = solve(p, rrf_scores=rr, top_k=5, consensus_rid=consensus, qtext=g["q"])
    rids = [r[0] for r in top]
    if g["gold"] in rids:
        rec5 += 1
    t1 = by_id[rids[0]]
    if g["kind"] == "easy":
        n_sig += 1
        if t1["family"] != g["family"]:
            fam_sig += 1
    else:
        n_amb += 1
        if t1["family"] != g["family"]:
            fam_amb += 1
    if g["kind"] == "dims_metric":
        dims_q += 1
        if not set(g["dims"]) & set(t1["dims"]):
            dims_miss += 1
n = len(gold)
print({"n": n, "recall@5": round(rec5 / n, 3),
       "family_confusion_signal": round(fam_sig / n_sig, 3),
       "family_confusion_ambig": round(fam_amb / n_amb, 3),
       "dims_miss": round(dims_miss / dims_q, 3)})
