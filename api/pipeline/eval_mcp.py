#!/usr/bin/env python3
"""MCP 경유 eval: gold 107 + garbage 5를 /mcp search_reports로 실행.

현행 data/eval/e2e.json 지표와 나란히 기록 + vector_weight 스윕 {0.5, 0.7, 0.85}.
0.7이 최대 recall@5와 0.03 이내면 0.7 유지, 초과로 뒤지면 최댓값 채택.

  uv run python tools/eval_mcp.py [--url http://localhost:8321/mcp] [--top-k 5]

출력: data/eval/eval_mcp.json
"""
import argparse
import json
import sys
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
OUT = BASE_DIR / "data" / "eval" / "eval_mcp.json"

GARBAGE = ["어제 우리 동네 강아지 산책로 알려줘", "오늘 점심 뭐 먹지",
           "asdkfj qwerty zzzz", "로또 번호 추천해줘", "날씨 어때"]
SWEEP = [0.5, 0.7, 0.85]


def rpc(url, method, params, rid):
    body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode())


def search(url, query, top_k, w, rid):
    d = rpc(url, "tools/call", {"name": "search_reports",
                                "arguments": {"query": query, "top_k": top_k,
                                              "vector_weight": w}}, rid)
    err = d.get("error")
    if err:
        raise RuntimeError(f"MCP error: {err}")
    return json.loads(d["result"]["content"][0]["text"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://localhost:8321/mcp")
    ap.add_argument("--top-k", type=int, default=5)
    a = ap.parse_args()

    sys.path.insert(0, str(BASE_DIR))
    from eval_baseline import build_gold
    catalog = json.loads((BASE_DIR / "data" / "catalog" / "report_catalog_w2d.json").read_text(encoding="utf-8"))
    gold = build_gold(catalog)
    print(f"[eval_mcp] gold {len(gold)} + garbage {len(GARBAGE)}, url={a.url}")

    rid = 0
    sweep = {}
    per_w_details = {}
    for w in SWEEP:
        rec5 = 0
        det = []
        for g in gold:
            rid += 1
            try:
                t = search(a.url, g["q"], a.top_k, w, rid)
                rids = [c["report_id"] for c in t["candidates"] if not c["dropped"]]
                hit = g["gold"] in rids
                rec5 += hit
                det.append({"q": g["q"][:40], "gold": g["gold"], "hit": hit,
                            "top1": rids[0] if rids else None})
            except Exception as e:
                det.append({"q": g["q"][:40], "gold": g["gold"], "error": str(e)[:100]})
        sweep[str(w)] = round(rec5 / len(gold), 3)
        per_w_details[str(w)] = det
        print(f"[eval_mcp] w={w} recall@{a.top_k}={sweep[str(w)]}")

    best_w = max(SWEEP, key=lambda w: sweep[str(w)])
    keep_07 = sweep["0.7"] >= sweep[str(best_w)] - 0.03
    chosen = 0.7 if keep_07 else best_w

    # garbage: 점수 낮음 + dropped 다수 육안 확인용
    gb = []
    for q in GARBAGE:
        rid += 1
        try:
            t = search(a.url, q, a.top_k, chosen, rid)
            cands = t["candidates"]
            gb.append({"q": q,
                       "max_score": max([(c["stage_score"] or 0) for c in cands], default=0),
                       "n_dropped": sum(1 for c in cands if c["dropped"]),
                       "top1": cands[0]["report_id"] if cands else None})
        except Exception as e:
            gb.append({"q": q, "error": str(e)[:100]})

    out = {"sweep_recall": sweep, "best_w": best_w, "chosen_w": chosen,
           "keep_07": keep_07, "n_gold": len(gold), "top_k": a.top_k,
           "garbage": gb, "details": per_w_details[str(chosen)]}
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[eval_mcp] chosen_w={chosen} (best={best_w}) -> {OUT}")


if __name__ == "__main__":
    main()
