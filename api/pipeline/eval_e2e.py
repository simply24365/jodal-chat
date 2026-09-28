#!/usr/bin/env python3
"""P5: end-to-end 측정. dev /api/chat 호출 (LLM 0회 경로라 cheap).
gold 합성셋 + garbage set. 지표:
  top1_acc (kind=reports & top1==gold), family_confusion_e2e,
  nomatch_on_answerable (gold인데 no_match 비율),
  garbage_no_match (garbage의 no_match 비율, 높을수록 좋음),
  clarify_rate, p50/p95 latency, llm_calls (=0 검증용 path 집계).
출력: data/eval/e2e.json
"""
import json
import time
import urllib.request
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CATALOG = {r["report_id"]: r for r in
           json.loads((BASE_DIR / "data" / "catalog" / "report_catalog_w2d.json").read_text(encoding="utf-8"))}
OUT = BASE_DIR / "data" / "eval" / "e2e.json"
URL = "http://localhost:8321/api/chat"

GARBAGE = ["어제 우리 동네 강아지 산책로 알려줘", "오늘 점심 뭐 먹지",
           "asdkfj qwerty zzzz", "로또 번호 추천해줘", "날씨 어때"]


def ask(q, action=None, qs=None):
    body = {"messages": [{"role": "user", "content": q}]}
    if action:
        body["action"] = action
    if qs:
        body["query_state"] = qs
    t0 = time.monotonic()
    req = urllib.request.Request(URL, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        d = json.loads(r.read().decode())
    return d, time.monotonic() - t0


def main():
    import sys
    sys.path.insert(0, str(BASE_DIR))
    from eval_baseline import build_gold
    catalog = list(CATALOG.values())
    gold = build_gold(catalog)
    print(f"[P5] gold {len(gold)} + garbage {len(GARBAGE)}")

    res = {"gold": [], "garbage": []}
    lat = []
    for g in gold:
        try:
            d, dt = ask(g["q"])
            lat.append(dt)
            top1 = (d.get("reports") or [{}])[0].get("report_id")
            res["gold"].append({"q": g["q"][:40], "gold": g["gold"],
                                "kind": d.get("kind"), "path": d.get("path"),
                                "top1": top1,
                                "fam_ok": (CATALOG.get(top1) or {}).get("family") == g["family"] if top1 else None})
        except Exception as e:
            res["gold"].append({"q": g["q"][:40], "gold": g["gold"], "error": str(e)[:100]})
    for q in GARBAGE:
        try:
            d, dt = ask(q)
            lat.append(dt)
            res["garbage"].append({"q": q, "kind": d.get("kind"), "path": d.get("path")})
        except Exception as e:
            res["garbage"].append({"q": q, "error": str(e)[:100]})

    g = [x for x in res["gold"] if "error" not in x]
    n = len(g)
    top1 = sum(1 for x in g if x["kind"] == "reports" and x["top1"] == x["gold"])
    fam_ok = [x for x in g if x["fam_ok"] is not None]
    nomatch = sum(1 for x in g if x["kind"] == "no_match")
    gb = [x for x in res["garbage"] if "error" not in x]
    lat_sorted = sorted(lat)
    out = {
        "n_gold": n,
        "top1_acc": round(top1 / n, 3),
        "family_confusion_e2e": round(1 - sum(x["fam_ok"] for x in fam_ok) / len(fam_ok), 3) if fam_ok else None,
        "nomatch_on_answerable": round(nomatch / n, 3),
        "clarify_rate": round(sum(1 for x in g if x["kind"] == "clarify") / n, 3),
        "garbage_no_match": round(sum(1 for x in gb if x["kind"] == "no_match") / len(gb), 3) if gb else None,
        "latency_p50": round(lat_sorted[len(lat_sorted) // 2], 1),
        "latency_p95": round(lat_sorted[int(len(lat_sorted) * 0.95)], 1),
        "paths": dict(Counter(x.get("path", "?") for x in g)),
        "details": res,
    }
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in out.items() if k != "details"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
