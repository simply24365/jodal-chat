"""search_reports / get_report_detail — 조달 통계보고서 131건 검색.

검색 파이프라인 (mcp/ Worker 의 JS 재구현 → 기존 Python 구현으로 복귀):

    slot_parser.parse(q)        슬롯 파싱 (family/dims/metric/concept)
        │
    search_hybrid.ranks(q)      BM25(bm25s+kiwipiepy) + Vector(Jina) 각각 랭킹
        │
    search_hybrid.rrf_scores    가중 RRF (1-w)/(K+rb) + w/(K+rv)
        │
    stage_solve.solve           S0~S5 결정적 스테이지 솔버
        │
    → candidates (score 내림차순)

mcp/ 와 달리 토크나이저가 Worker 의 "kiwi 근사" 정규식이 아니라 진짜 kiwipiepy 이며,
stage_solve 에는 Worker 버전에 없던 S1.5(이름 직접언급 보너스)이 있다.

Vector 강등: Jina 크레딧 소진 등으로 임베딩이 실패하면 예외 대신 BM25 단독(w=0)으로
내려가되, 결과에 degraded 플래그로 근거를 남긴다 — 조용히 품질이 떨어지면 안 된다.
"""

from __future__ import annotations

from typing import Any

from ..retrieval import catalog as cat
from ..retrieval import embed, search_hybrid, slot_parser, stage_solve
from ..retrieval.embed import EmbeddingError

POOL = 50
MAX_TOP_K = 30

SEARCH_REPORTS_DESC = (
    "131개 조달 통계보고서 메타데이터 검색. 원하는 통계를 문장으로 주면 후보 object[]를 "
    "score 내림차순으로 반환. dims가 한국어 Split(~별)일 때만 의미 있음. "
    "'소관구분'은 소속(국가기관/공기업…)을 뜻하며 개별 기관 조회가 아님. "
    "stage_score는 참고용이며 낮아도 dims_coverage/metric_hit/concept_hits가 있으면 유력 후보. "
    "점수·탈락 이유로 최종 선택·되묻기를 호출 측이 판단할 것. "
    "report_id를 지어내지 말 것. candidates에 없는 ID 사용 금지. "
    "catalog_ver가 바뀌면 캐시를 버리고 재조회할 것."
)
GET_REPORT_DETAIL_DESC = (
    "보고서 1건의 상세(입력조건·지표·차원·개념·바로열기 링크). 보고서명 또는 report_id 중 하나로 조회. "
    "search_reports 후보 중 '자세히 볼' 1건씩 조회용. 131건 통째 조회 불가."
)


def _clamp_top_k(v: Any) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 10
    return max(1, min(MAX_TOP_K, n))


def _norm(s: Any) -> str:
    return "".join(str(s if s is not None else "").split())


def _signals(rid: str, parsed: dict, bm_rank: dict, vec_rank: dict) -> dict:
    """mcp/ 의 mcpSignals() 이식."""
    rec = cat.catalog().get(rid, {})
    qd = parsed.get("dims_explicit") or parsed.get("dims") or []
    all_m = rec.get("metrics") or []
    qm = parsed.get("metric") or []
    fam = parsed.get("family") or {}
    qn = _norm(parsed.get("q") or "")
    nm = _norm(rec.get("보고서명") or "")
    return {
        "family": "none"
        if not fam.get("value")
        else ("match" if rec.get("family") == fam["value"] else "mismatch"),
        "dims_coverage": round(
            len([d for d in qd if d in (rec.get("dims") or [])]) / len(qd), 2
        )
        if qd
        else 1.0,
        "metric_hit": [m for m in qm if any(m in x or x in m for x in all_m)],
        "concept_hits": [
            cid
            for cid in (parsed.get("concepts") or [])
            if cid in cat.matching_concept_ids(rec)
        ],
        "name_mention": bool(nm and qn and nm in qn),
        "bm25_rank": bm_rank.get(rid),
        "vec_rank": vec_rank.get(rid),
    }


def search_reports(
    query: str,
    top_k: Any = 10,
    vector_weight: Any = None,
) -> dict:
    q = str(query or "").strip()
    if not q:
        raise ValueError("query required")
    top_k = _clamp_top_k(top_k)
    pool = POOL

    parsed = slot_parser.parse(q)
    parsed["q"] = q

    degraded = None
    if embed.breaker_open():
        # 회로가 열려 있으면 네트워크 호출 없이 바로 강등.
        bm_rank, vec_rank = search_hybrid.ranks(q, pool, embed_query=False)
        scores, w, k = search_hybrid.bm25_only_scores(bm_rank, pool)
        degraded = "vector_circuit_open: Jina 직전 영구 실패로 BM25 단독"
    else:
        try:
            bm_rank, vec_rank = search_hybrid.ranks(q, pool)
            scores, w, k = search_hybrid.rrf_scores(bm_rank, vec_rank, pool, vector_weight)
        except EmbeddingError as e:
            # Vector 경로 강등. BM25만으로 간다 — 단 결과에 근거를 남긴다.
            degraded = f"vector_unavailable: {e}"
            bm_rank, vec_rank = search_hybrid.ranks(q, pool, embed_query=False)
            scores, w, k = search_hybrid.bm25_only_scores(bm_rank, pool)

    bm_top = list(bm_rank)[:1]
    v_top = list(vec_rank)[:1]
    consensus = bm_top[0] if bm_top and v_top and bm_top[0] == v_top[0] else None

    staged = stage_solve.solve(
        parsed,
        rrf_scores=scores,
        pool=pool,
        top_k=max(top_k, 30),
        consensus_rid=consensus,
        qtext=q,
    )

    cat_all = cat.catalog()
    candidates = []
    for rid, s, log in staged[:top_k]:
        rec = cat_all.get(rid, {})
        dropped = s <= -1e8
        reasons = [x.get("reason") for x in (log or []) if x.get("reason")]
        official_id = rec.get("official_id")
        candidates.append(
            {
                "report_id": rid,
                "name": rec.get("보고서명") or rid,
                "official_id": official_id or None,
                "stage_score": None if dropped else round(s, 4),
                "signals": _signals(rid, parsed, bm_rank, vec_rank),
                "dropped": dropped,
                "drop_reason": "; ".join(reasons) if dropped else None,
                "views": rec.get("조회수"),
                "links": {"move": cat.report_link(official_id) if official_id else None},
            }
        )

    return {
        "query_slots": {
            "family": parsed.get("family"),
            "dims": parsed.get("dims"),
            "dims_explicit": parsed.get("dims_explicit"),
            "excluded_dims": parsed.get("excluded_dims"),
            "channel": parsed.get("channel"),
            "item_scope": parsed.get("item_scope"),
            "metric": parsed.get("metric"),
            "visual": parsed.get("visual"),
            "concepts": parsed.get("concepts"),
        },
        "candidates": candidates,
        "scoring": {
            "method": "weighted_rrf+stage",
            "vector_weight": w,
            "rrf_k": k,
            "pool": pool,
            **({"degraded": degraded} if degraded else {}),
        },
        "catalog_ver": cat.CATALOG_VER,
    }


def _resolve_report_id(report_id: Any, report_name: Any) -> str:
    """mcp/ 의 resolveReportId() 이식."""
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
        "reptId": rec.get("mstrReptIdVal") or rid,
        "desc": rec.get("desc") or "",
        "conds": cat.cond_names(rec),
        "metrics": rec.get("metrics") or [],
        "dims": rec.get("dims") or rec.get("dimensions") or [],
        "concepts": cat.matching_concept_ids(rec),
        "family": rec.get("family"),
        "links": {
            "move": cat.report_link(official_id) if official_id else None,
            "direct": cat.direct_link(rid),
        },
        "catalog_ver": cat.CATALOG_VER,
    }
