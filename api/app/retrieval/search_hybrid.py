#!/usr/bin/env python3
"""Hybrid 검색 (BM25 + Vector → RRF).

  from app.retrieval.search_hybrid import ranks, rrf_scores
  bm, vec = ranks("서울 공공기관 노트북 계약금액")
  rrf, w, k = rrf_scores(bm, vec, weight=0.7)

개념:
- BM25 쿼리만 개념 확장(태깅) 적용, Vector 쿼리는 원문 유지.
- rank는 1-base. dict 미포함은 pool+1 로 간주(rrf_scores 가 처리).
- Vector 실패 시 조용히 강등하지 않는다. ranks() 가 예외를 올리고,
  호출부(app/tools/search.py)가 BM25 폴백 여부를 결정한다.

CLI: uv run python app/retrieval/search_hybrid.py "질문" [--top-k 10]
"""
import argparse
import json
import os
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
BASE_DIR = API_ROOT / "data"
IDX_DIR = BASE_DIR / "index"
BM25_DIR = IDX_DIR / "bm25"
VEC_PATH = IDX_DIR / "vectors.json"
META_PATH = IDX_DIR / "meta.json"
KEEP_TAGS = {"NNG", "NNP", "VV", "VA", "SL", "SN"}

_meta = json.loads(META_PATH.read_text(encoding="utf-8"))
_RRF_K = _meta.get("rrf_k", 60)
N_DOCS = int(_meta.get("n_docs", 131))
VECTOR_WEIGHT_DEFAULT = 0.7
_retriever = None
_kiwi = None
_vecs = None
_rids = None
_np = None


def _lazy():
    global _retriever, _kiwi, _vecs, _rids, _np
    if _retriever is None:
        import bm25s
        from kiwipiepy import Kiwi
        import numpy as np
        _np = np
        _kiwi = Kiwi()
        _retriever = bm25s.BM25.load(str(BM25_DIR), load_corpus=True)
        _vecs = json.loads(VEC_PATH.read_text(encoding="utf-8"))
        _rids = list(_vecs.keys())
        _assert_index_consistent()


def _assert_index_consistent() -> None:
    """인덱스가 자기 메타데이터와 맞는�� 확인.

    build_hybrid_index.py 의 재개 로직("있으면 스킵") 때문에 모델/차원을 바꿔
    부분 재임베딩하면 **옛 벡터와 새 벡터가 섞인다**. 그러면 검색 결과가
    조용히 무의미해지고, 어느 원인인지 찾기 어렵다. 여기서 즉시 드러낸다.
    """
    if not _vecs:
        raise RuntimeError(
            f"벡터 인덱스가 비어 있다: {VEC_PATH} — cd api && uv run python "
            "pipeline/build_hybrid_index.py 로 생성"
        )
    want = int(_meta.get("dims") or 0)
    bad = sorted({len(v) for v in _vecs.values()} - {want})
    if bad:
        raise RuntimeError(
            f"벡터 차원 불일치: meta.dims={want} 인데 {bad} 길이 벡터가 있다. "
            f"{VEC_PATH} 를 지우고 pipeline/build_hybrid_index.py 로 전량 재임베딩할 것."
        )
    n_docs = _meta.get("n_docs")
    if n_docs and len(_vecs) != int(n_docs):
        # 전부 임베딩되기 전인 상태일 수 있다(빌드 진행 중). 경고만.
        import logging

        logging.getLogger("custom_chat.retrieval").warning(
            "벡터 %d/%s 건 — 임베딩 미완료. search_reports 는 BM25 만 쓴다.",
            len(_vecs),
            n_docs,
        )


def _tok(text):
    return [t.form for t in _kiwi.tokenize(text)
            if t.tag in KEEP_TAGS and len(t.form.strip()) > 0]


def _embed_query(q):
    from .embed import embed

    return embed([q], _meta.get("task_query", "retrieval.query"))[0]


def query(q, top_k=10, pool=50):
    """RRF 융합 Top-K (w=0.5 고정). 간이 확인용.
    실서비스는 weighted RRF(w tunable)가 필요하므로 ranks() + rrf_scores() 를 쓴다."""
    bm_rank, vec_rank = ranks(q, pool)
    scored = []
    for rid in set(bm_rank) | set(vec_rank):
        rb = bm_rank.get(rid, pool + 1)
        rv = vec_rank.get(rid, pool + 1)
        scored.append((rid, 1 / (_RRF_K + rb) + 1 / (_RRF_K + rv), rb, rv))
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]


def ranks(q, pool=50, embed_query=True):
    """BM25/Vector 각자의 랭킹을 분리 반환. 툴은 w 를 가변으로 줘야 한다.

    반환: (bm_rank, vec_rank) — rid → 1-base rank. 미포함은 dict 에 없다.
    embed_query=False 면 BM25만 돌리고 vec_rank={} (Jina 미설정/크레딧 소진 강등).
    그 밖의 실패는 조용히 강등하지 않고 예외를 올린다 — 호출부가 판단한다.
    """
    _lazy()
    from .concept_tagger import expand_query

    docs, _ = _retriever.retrieve(
        [_tok(expand_query(q))], k=min(pool, N_DOCS), show_progress=False
    )
    norm = [(d.get("text", d) if isinstance(d, dict) else d) for d in docs[0]]
    bm_rank = {str(d): i + 1 for i, d in enumerate(norm)}
    if not embed_query:
        return bm_rank, {}

    qv = _np.array(_embed_query(q), dtype="float32")
    M = _np.array([_vecs[r] for r in _rids], dtype="float32")
    M = M / (_np.linalg.norm(M, axis=1, keepdims=True) + 1e-9)
    qv = qv / (_np.linalg.norm(qv) + 1e-9)
    order = _np.argsort(-(M @ qv))[:pool]
    return bm_rank, {_rids[i]: r + 1 for r, i in enumerate(order)}


def rrf_scores(bm_rank, vec_rank, pool=50, weight=None):
    """가중 RRF: score=(1-w)/(K+rb) + w/(K+rv). 미포함 rank 는 pool+1."""
    w = _vector_weight(weight)
    k = _RRF_K
    out = {}
    for rid in set(bm_rank) | set(vec_rank):
        rb = bm_rank.get(rid, pool + 1)
        rv = vec_rank.get(rid, pool + 1)
        out[rid] = (1 - w) / (k + rb) + w / (k + rv)
    return out, w, k


def bm25_only_scores(bm_rank, pool=50):
    """Vector 경로 강등용: w=0 RRF (=순수 BM25 RRF)."""
    return {rid: 1 / (_RRF_K + r) for rid, r in bm_rank.items()}, 0.0, _RRF_K


def _vector_weight(weight=None):
    if weight is None:
        raw = os.environ.get("VECTOR_WEIGHT", "")
    else:
        raw = weight
    try:
        n = float(raw)
    except (TypeError, ValueError):
        return VECTOR_WEIGHT_DEFAULT
    if n != n:  # NaN
        return VECTOR_WEIGHT_DEFAULT
    return max(0.0, min(1.0, n))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--top-k", type=int, default=10)
    a = ap.parse_args()
    for rid, s, rb, rv in query(a.question, top_k=a.top_k):
        print(f"{rid} rrf={s:.4f} bm25={rb} vec={rv}")


if __name__ == "__main__":
    import sys

    # CLI 단독 실행 전용: 평평한 import 로도 돌리려고 자기 디렉터를 경로에 넣는다.
    # 앱 안에서는 __main__ 이 아니므로 이 분기를 타지 않는다.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
