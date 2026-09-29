"""자연 질의 eval — BM25 : vector 가중 3단 설정 비교.

  0:1   vector 0,  bm25 1  → 순수 BM25 (모델 불필요)
  5:5   vector .5, bm25 .5  → 균형
  7:3   vector .7, bm25 .3  ← search.py 의 VECTOR_WEIGHT_DEFAULT

build_gold() 는 '{보고서명} 보여줘' 를 만들어서 정의상 어휘적 질의가 되고
BM25 에 유리했다. 이 스크립트는 data/eval/natural_queries.json — 사람이 실제로
치는 문장(기관명·시각화요구·기업유형·비교·절차질문·도메인밖)을 쓴다.

임베더는 e5-small(ONNX, 386MB) 만. 이 박스에서 1GB 이상 모델은 OOM 으로 죽었다.

  uv run python pipeline/eval_natural.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

try:
    # POSIX 전용 (ru_maxrss). Windows 에는 없다 — 메모리 측정은 선택 기능.
    import resource  # noqa: F401
except ImportError:
    resource = None

API = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API))
sys.path.insert(0, str(API / "app" / "retrieval"))

import numpy as np  # noqa: E402

from app.retrieval import catalog as cat  # noqa: E402
from app.retrieval import search_hybrid, slot_parser, stage_solve  # noqa: E402
from app.retrieval.onnx_embed import LocalEmbedder  # noqa: E402

EVAL = json.loads((API / "data" / "eval" / "natural_queries.json").read_text(encoding="utf-8"))
QUERIES = EVAL["queries"]
POOL, K = 50, 5
WEIGHTS = [("0:1  BM25 only", 0.0), ("5:5  균형", 0.5), ("7:3  현재 기본", 0.7)]


def matches(rec: dict, cond: dict) -> bool:
    if "is_visual" in cond and bool(rec.get("is_visual")) != cond["is_visual"]:
        return False
    if "dims_contains" in cond and cond["dims_contains"] not in (rec.get("dims") or []):
        return False
    if "family_contains" in cond and cond["family_contains"] not in str(rec.get("family") or ""):
        return False
    if "cols_contains" in cond:
        cols = [c.get("name", "") for c in (rec.get("columns") or [])]
        if not any(cond["cols_contains"] in c for c in cols):
            return False
    return True


def top5(item: dict, w: float, m: LocalEmbedder | None, rids, dv) -> list[str]:
    q = item["q"]
    bm_rank, _ = search_hybrid.ranks(q, POOL, embed_query=False)
    if w > 0 and m is not None:
        sims = dv @ m.embed_query(q)
        v_rank = {rids[i]: r + 1 for r, i in enumerate(np.argsort(-sims)[:POOL])}
        scores, _, _ = search_hybrid.rrf_scores(bm_rank, v_rank, POOL, w)
    else:
        scores, _, _ = search_hybrid.bm25_only_scores(bm_rank, POOL)
    parsed = slot_parser.parse(q)
    staged = stage_solve.solve(parsed, rrf_scores=scores, pool=POOL, top_k=max(K, 30), qtext=q)
    return [r for r, s, _ in staged[:K] if s > -1e8]


def evaluate(m, rids, dv, use_vector: bool) -> list[dict]:
    cat_all = cat.catalog()
    rows = []
    for name, w in WEIGHTS:
        eff = w if use_vector else 0.0
        h1 = h5 = cand = grab = n_ans = n_none = 0
        per_type: dict[str, list[int]] = {}
        misses: list[str] = []
        for item in QUERIES:
            alive = top5(item, eff, m, rids, dv)
            cand += len(alive)
            gold = set(item.get("gold") or [])
            cond = item.get("gold_any")
            if gold or cond:
                n_ans += 1
                ok5 = bool(gold & set(alive)) if gold else any(
                    matches(cat_all.get(r, {}), cond) for r in alive
                )
                if gold:
                    ok1 = alive[:1] == [next(iter(gold))] if len(gold) == 1 else False
                else:
                    ok1 = bool(alive) and matches(cat_all.get(alive[0], {}), cond)
                h1 += ok1
                h5 += ok5
                per_type.setdefault(item.get("type", "?"), []).append(int(ok5))
                if not ok5:
                    misses.append(item["q"][:34])
            else:
                n_none += 1
                grab += bool(alive)
        rows.append(
            {
                "weight": name,
                "hit@1": round(h1 / n_ans, 3),
                "hit@5": round(h5 / n_ans, 3),
                "cand@5(과잉매칭)": round(cand / len(QUERIES), 2),
                f"grab(답없는질의)": f"{grab}/{n_none}",
                "유형별 hit@5": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(per_type.items())},
                "5문배락": misses,
            }
        )
    return rows


def main() -> None:
    n_ans = sum(1 for x in QUERIES if x.get("gold") or x.get("gold_any"))
    n_none = len(QUERIES) - n_ans
    print(f"자연 질의 {len(QUERIES)}개 · 정답있음 {n_ans} · 답없음 {n_none}\n")

    print("[모델 미로드] 0:1 — 순수 BM25 기준선")
    for r in evaluate(None, None, None, False):
        print(json.dumps(r, ensure_ascii=False))

    print("\n[e5-small ONNX 386MB 로드]")
    m = LocalEmbedder()
    t0 = time.time()
    m.load()
    load_s = time.time() - t0
    r0 = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
          if resource else 0.0)
    rows = [
        json.loads(x)
        for x in (API / "data" / "catalog" / "search_docs.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    rids = np.array([r["report_id"] for r in rows])
    t0 = time.time()
    dv = m.embed_passages([r["text"] for r in rows], batch=8)
    idx_s = time.time() - t0
    rss = (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 - r0
           if resource else 0.0)
    print(f"  로드 {load_s:.1f}s · 131건 임베딩 {idx_s:.1f}s · "
          + (f"RSS +{rss:.0f}MB" if resource else "RSS 측정 생략 (Windows)"))

    for r in evaluate(m, rids, dv, True):
        print(json.dumps(r, ensure_ascii=False))

    lat = []
    for item in QUERIES:
        t = time.time()
        m.embed_query(item["q"])
        lat.append((time.time() - t) * 1000)
    lat.sort()
    print(f"\n쿼리 임베딩 지연 — 중앙 {lat[len(lat) // 2]:.0f}ms · p90 {lat[int(len(lat) * 0.9)]:.0f}ms")


if __name__ == "__main__":
    main()
