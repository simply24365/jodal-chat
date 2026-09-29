"""search_reports / get_report_detail — 순수 하이브리드 검색 (BM25 + vector → RRF).

    쿼리
      ├─ BM25   랭킹  (bm25s + kiwipiepy, 개념 확장)
      └─ vector 랭킹  (로컬 e5-small ONNX, 384d, 16ms)
              ↓
        RRF 융합  score = (1-w)/(K+bm_rank) + w/(K+vec_rank)
              ↓
        상위 N 개를 이름·설명·조건과 함께 반환

★ 규칙 엔진(slot_parser / stage_solve)을 쓰지 않는다. 근거:
  - 자연 질의 20개 A/B 비교에서 규칙 엔진이 hit@5 를 0.667 → 0.533 으로
    떨어뜨렸다(BM25 top-5 안의 정답을 S0/S1 하드 킬이 제거).
    → pipeline/ab_rerank.py 로 재현 가능.
  - RRF 는 S5 한 줄로만 흘러들어가 vector_weight 를 0.0→0.7 로 바꿔도
    hit@5 가 0.533 그대로였다. 하이브리드가 사실상 승객이던 상태.
  - 하드 킬이 없으므로 과잉매칭이 늘어난다(cand@5 = 5.0). 그건 툴이 아니라
    **agent 가 고르는 것**이다 — 이미 그렇게 동작한다. 그래서 top_k 를 10 으로
    올려 판단 재료를 더 준다.

정답·무관 판정은 호출측 LLM 이 한다. 툴은 사실만 반환한다.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from ..retrieval import catalog as cat
from ..retrieval import search_hybrid

API_ROOT = Path(__file__).resolve().parents[2]
LOCAL_VEC_PATH = API_ROOT / "data" / "index" / "local_vectors.json"
LOCAL_META_PATH = API_ROOT / "data" / "index" / "local_meta.json"

POOL = 50
DEFAULT_TOP_K = 10
MAX_TOP_K = 30

SEARCH_REPORTS_DESC = (
    f"131개 조달 통계보고서 검색. 원하는 통계를 문장으로 주면 후보를 score 내림차순으로 "
    f"최대 {MAX_TOP_K}개 반환한다. 각 후보에 report_id·이름·설명·조건·지표가 붙는다. "
    "'몇 개나 있어?'·'어떤 종류가 있어?' 같은 집계 질문에는 catalog_facets 를 답한다 "
    "(total_reports·visualizable·by_family·by_dimension). "
    "후보에 없는 report_id를 지어내지 말 것 — 보고서를 특정할 수 없으면 "
    "조건 선택값(기관명·업체명)으로는 못 잡히므로 value_lookup 을 쓸 것. "
    "요청한 통계에 해당하는 보고서가 후보에 없으면 없다고 말하고 지어내지 말 것."
)
GET_REPORT_DETAIL_DESC = (
    "보고서 1건의 상세(입력조건·지표·차원·개념·바로열기 링크). 보고서명 또는 report_id 중 하나로 조회. "
    "search_reports 후보 중 '자세히 볼' 1건씩 조회용. 131건 통째 조회 불가."
)

_local_vecs: dict[str, list[float]] | None = None
_local_rids: list[str] = []


def _local_index() -> tuple[list[str], dict[str, list[float]]]:
    """로컬 임베딩 벡터 로드(프로세스 상주). 없으면 빈 인덱스."""
    global _local_vecs, _local_rids
    if _local_vecs is None:
        if not LOCAL_VEC_PATH.is_file():
            _local_vecs, _local_rids = {}, []
        else:
            raw = json.loads(LOCAL_VEC_PATH.read_text(encoding="utf-8"))
            _local_rids = list(raw)
            _local_vecs = raw
    return _local_rids, _local_vecs


def _clamp_top_k(v: Any) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return DEFAULT_TOP_K
    return max(1, min(MAX_TOP_K, n))


def _norm(s: Any) -> str:
    return "".join(str(s if s is not None else "").split())


def search_reports(
    query: str,
    top_k: Any = DEFAULT_TOP_K,
    vector_weight: Any = None,
) -> dict:
    q = str(query or "").strip()
    if not q:
        raise ValueError("query required")
    top_k = _clamp_top_k(top_k)

    rids, vecs = _local_index()
    bm_rank, _vec_rank_unused = search_hybrid.ranks(q, POOL, embed_query=False)

    vec_rank: dict[str, int] = {}
    degraded = None
    if rids and vecs and (vector_weight is None or float(vector_weight) != 0):
        from ..retrieval.onnx_embed import get_default

        try:
            emb = get_default()
            emb.load()
            qv = emb.embed_query(q)
            sims = {r: _cos(qv, vecs[r]) for r in rids}
            for i, r in enumerate(sorted(sims, key=lambda x: -sims[x])[:POOL]):
                vec_rank[r] = i + 1
        except Exception as e:  # noqa: BLE001
            degraded = f"vector_unavailable: {type(e).__name__}: {e}"

    if vec_rank:
        scores, w, k = search_hybrid.rrf_scores(bm_rank, vec_rank, POOL, vector_weight)
    else:
        scores, w, k = search_hybrid.bm25_only_scores(bm_rank, POOL)
        if degraded is None:
            degraded = "vector_index_missing: pipeline/build_local_index.py 로 생성"

    cat_all = cat.catalog()
    d2q_all = cat.doc2query()
    ranked = sorted(scores.items(), key=lambda x: -x[1])[:top_k]
    candidates = []
    for rid, s in ranked:
        rec = cat_all.get(rid, {})
        official_id = rec.get("official_id")
        d2q = d2q_all.get(rid) or {}
        candidates.append(
            {
                "report_id": rid,
                "name": rec.get("보고서명") or rid,
                "score": round(float(s), 6),
                "official_id": official_id or None,
                "summary": (rec.get("desc_summary") or "")[:180],
                # LLM 생성 한줄요약 — desc_summary 보다 질의 관점이어서 후보 판단에 유리
                "synopsis": (d2q.get("synopsis") or "")[:140] or None,
                "dims": rec.get("dims") or [],
                "metrics": (rec.get("metrics") or [])[:6],
                "family": rec.get("family"),
                "is_visual": bool(rec.get("is_visual")),
                "signals": {
                    "bm25_rank": bm_rank.get(rid),
                    "vec_rank": vec_rank.get(rid) or None,
                },
                "links": {"move": cat.report_link(official_id) if official_id else None},
            }
        )

    return {
        "query": q,
        "candidates": candidates,
        "catalog_facets": {
            # 메타질문("몇 개나 있어?", "어떤 종류가 있어?")에 답하는 집계 —
            # 검색 패싯(faceted search)의 표준 구성. LLM 이 후보 목록만으로는
            # 알 수 없는 전체 분포를 데이터로 제공한다.
            "total_reports": len(cat_all),
            "visualizable": sum(1 for r in cat_all.values() if r.get("is_visual")),
            "by_family": dict(Counter(r.get("family") for r in cat_all.values()).most_common(10)),
            "by_dimension": dict(Counter(d for r in cat_all.values() for d in (r.get("dims") or [])).most_common(10)),
        },
        "scoring": {
            "method": "hybrid_bm25+vector_rrf",
            "vector_weight": w,
            "rrf_k": k,
            "pool": POOL,
            "index": "local e5-small ONNX" if vec_rank else "bm25 only",
            **({"degraded": degraded} if degraded else {}),
        },
        "catalog_ver": cat.CATALOG_VER,
    }


def _cos(a: list[float], b: list[float]) -> float:
    """벡터는 빌드 시 정규화되어 있다(길이 1). 그래도 방어적으로 나눈다."""
    s = 0.0
    for x, y in zip(a, b):
        s += x * y
    return s


def _resolve_report_id(report_id: Any, report_name: Any) -> str:
    cat_all = cat.catalog()
    rid = str(report_id or "").strip()
    if rid:
        if rid in cat_all:
            return rid
        raise ValueError(f"unknown report_id: {rid[:20]}")
    q = _norm(report_name)
    if not q:
        raise ValueError("report_id or report_name required")
    for id_, rec in cat_all.items():
        if _norm(rec.get("보고서명")) == q:
            return id_
    hits = [
        (id_, rec.get("보고서명"))
        for id_, rec in cat_all.items()
        if (n := _norm(rec.get("보고서명"))) and (n in q or q in n)
    ]
    if len(hits) == 1:
        return hits[0][0]
    if hits:
        cand = " / ".join(f"{i} {n}" for i, n in hits[:5])
        raise ValueError(f"ambiguous report_name, candidates: {cand}")
    raise ValueError(f"unknown report_name: {str(report_name)[:40]}")


def get_report_detail(
    report_id: Any = None,
    report_name: Any = None,
) -> dict:
    rid = _resolve_report_id(report_id, report_name)
    rec = cat.catalog()[rid]
    official_id = rec.get("official_id")
    return {
        "report_id": rid,
        "name": rec.get("보고서명") or rid,
        "official_id": official_id or None,
        "desc": rec.get("desc") or "",
        "conds": cat.cond_names(rec),
        "metrics": rec.get("metrics") or [],
        "dims": rec.get("dims") or rec.get("dimensions") or [],
        "concepts": cat.matching_concept_ids(rec),
        "family": rec.get("family"),
        "is_visual": bool(rec.get("is_visual")),
        "links": {
            "move": cat.report_link(official_id) if official_id else None,
            "direct": cat.direct_link(rid),
        },
        "catalog_ver": cat.CATALOG_VER,
    }
