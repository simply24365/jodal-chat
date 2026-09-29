"""순수 하이브리드(B) vs 현행 규칙엔진(A) vs BM25 only(C) — 검색 경로 분리 측정.

A: slot_parser + stage_solve(S0~S5)  ← 이전 현행
B: 순수 하이브리드 BM25+vector→RRF    ← app/tools/search.py 의 현행 구현
C: 순수 BM25 → RRF(w=0)

질의는 data/eval/natural_queries.json (자연 문장 20개, 정답 15 / 답없음 5).
n=15 라 ±2~3질의는 노이즈다. 그래서 지표를 '몇 개 맞았나' 로 함께 본다.

  uv run python pipeline/ab_rerank.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

API = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API))
sys.path.insert(0, str(API / "app" / "retrieval"))

from app.tools import search as S  # noqa: E402
from app.retrieval import catalog as cat  # noqa: E402
from app.retrieval import search_hybrid, slot_parser, stage_solve  # noqa: E402

QUERIES = json.loads((API / "data" / "eval" / "natural_queries.json").read_text(encoding="utf-8"))["queries"]
POOL, K = 50, 10
VEC_PATH = API / "data" / "index" / "local_vectors.json"


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


def hits(item: dict, alive: list[str]) -> tuple[bool, bool]:
    cat_all = cat.catalog()
    gold = set(item.get("gold") or [])
    cond = item.get("gold_any")
    if gold:
        ok5 = bool(gold & set(alive))
        ok1 = alive[:1] == [next(iter(gold))] if len(gold) == 1 else False
    elif cond:
        ok5 = any(matches(cat_all.get(r, {}), cond) for r in alive)
        ok1 = bool(alive) and matches(cat_all.get(alive[0], {}), cond)
    else:
        return False, False
    return ok1, ok5


def variant(name: str, fn) -> None:
    h1 = h5 = cand = grab = n_ans = n_none = 0
    per_type: dict[str, list[int]] = {}
    miss: list[str] = []
    t0 = time.time()
    for item in QUERIES:
        alive = fn(item)
        cand += len(alive)
        gold = item.get("gold") or item.get("gold_any")
        if gold:
            n_ans += 1
            ok1, ok5 = hits(item, alive)
            h1 += ok1
            h5 += ok5
            per_type.setdefault(item.get("type", "?"), []).append(int(ok5))
            if not ok5:
                miss.append(item["q"][:30])
        else:
            n_none += 1
            grab += bool(alive)
    el = (time.time() - t0) / len(QUERIES) * 1000
    print(
        json.dumps(
            {
                "변형": name,
                "hit@1": round(h1 / n_ans, 3),
                f"hit@{K}": round(h5 / n_ans, 3),
                "적중수": f"{h5}/{n_ans}",
                f"cand@{K}": round(cand / len(QUERIES), 2),
                f"grab/{n_none}": grab,
                "질의당ms": round(el, 1),
                "유형별": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(per_type.items())},
            },
            ensure_ascii=False,
        )
    )


def _A(item):
    q = item["q"]
    bm, _ = search_hybrid.ranks(q, POOL, embed_query=False)
    vr = {}
    vecs = json.loads(VEC_PATH.read_text(encoding="utf-8"))
    from app.retrieval.onnx_embed import get_default

    e = get_default()
    e.load()
    qv = e.embed_query(q)
    sims = {r: sum(x * y for x, y in zip(qv, vecs[r])) for r in vecs}
    for i, r in enumerate(sorted(sims, key=lambda x: -sims[x])[:POOL]):
        vr[r] = i + 1
    sc, _, _ = search_hybrid.rrf_scores(bm, vr, POOL, 0.7)
    p = slot_parser.parse(q)
    st = stage_solve.solve(p, rrf_scores=sc, pool=POOL, top_k=K, qtext=q)
    return [r for r, s, _ in st[:K] if s > -1e8]


def _B(item):
    return [c["report_id"] for c in S.search_reports(item["q"], top_k=K)["candidates"]]


def _C(item):
    return [c["report_id"] for c in S.search_reports(item["q"], top_k=K, vector_weight=0)["candidates"]]


def main() -> None:
    print(f"질의 {len(QUERIES)} · 정답있음 {sum(1 for x in QUERIES if x.get('gold') or x.get('gold_any'))} "
          f"· 답없음 {sum(1 for x in QUERIES if not x.get('gold') and not x.get('gold_any'))} · top_k={K}\n")
    S.search_reports("워밍", top_k=1)  # 첫 로드 5s 는 측정에서 제외
    variant("A 규칙엔진+하이브리드 (구현)", _A)
    variant("B 순수 하이브리드 (현행 search.py)", _B)
    variant("C 순수 BM25 (vector_weight=0)", _C)


if __name__ == "__main__":
    main()
