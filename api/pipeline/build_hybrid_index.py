#!/usr/bin/env python3
"""W4-1: Hybrid 인덱스 빌드 (BM25 bm25s + Jina 임베딩). 원본 불변, 재개 지원.

입력: data/catalog/search_docs.jsonl (131)
출력: data/index/{bm25/, vectors.json, meta.json}
진행: data/index/embed_progress.json (완료 rid 스킵)

  uv run python tools/build_hybrid_index.py
"""
import json
import sys
import time
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

from app.retrieval.embed import dims as DIMS  # noqa: E402
from app.retrieval.embed import embed as _embed  # noqa: E402
from app.retrieval.embed import model as MODEL  # noqa: E402

BASE_DIR = API_ROOT / "data"
# 증강 문서 우선: doc2query·구보고서명 병기본(search_docs_aug.jsonl)이 있으면 그걸 읽고,
# 없으면 원본을 쓴다 (원본 불변 원칙 유지 — augment_docs.py 가 증강본을 생성한다).
_AUG = BASE_DIR / "catalog" / "search_docs_aug.jsonl"
JSONL_PATH = _AUG if _AUG.exists() else BASE_DIR / "catalog" / "search_docs.jsonl"
IDX_DIR = BASE_DIR / "index"
BM25_DIR = IDX_DIR / "bm25"
VEC_PATH = IDX_DIR / "vectors.json"
META_PATH = IDX_DIR / "meta.json"
PROG_PATH = IDX_DIR / "embed_progress.json"

BATCH = 16
RRF_K = 60
KEEP_TAGS = {"NNG", "NNP", "VV", "VA", "SL", "SN"}


def jina_embed(texts, task):
    """배치 빌드용 래퍼. 실패는 EmbeddingError → 중단(sys.exit)으로 전환."""
    from app.retrieval.embed import EmbeddingError

    try:
        return _embed(texts, task)
    except EmbeddingError as e:
        sys.exit(f"ABORT: {e}")


def main():
    import bm25s
    from kiwipiepy import Kiwi
    import kiwipiepy

    rows = [json.loads(l) for l in JSONL_PATH.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 131, f"문서 {len(rows)} != 131"
    IDX_DIR.mkdir(parents=True, exist_ok=True)

    kiwi = Kiwi()

    def tok(text):
        return [t.form for t in kiwi.tokenize(text)
                if t.tag in KEEP_TAGS and len(t.form.strip()) > 0]

    # BM25
    print("[W4] kiwi 토큰화 + bm25s 인덱싱...", flush=True)
    corpus_tokens = [tok(r["text"]) for r in rows]
    rids = [r["report_id"] for r in rows]
    retriever = bm25s.BM25()
    retriever.index(corpus_tokens, show_progress=False)
    retriever.save(str(BM25_DIR), corpus=rids)
    print(f"[W4] bm25 저장: {BM25_DIR}", flush=True)

    # Vectors (재개 지원)
    prog = json.loads(PROG_PATH.read_text(encoding="utf-8")) if PROG_PATH.exists() else {}
    vecs = json.loads(VEC_PATH.read_text(encoding="utf-8")) if VEC_PATH.exists() else {}

    # ★ 재개는 "있으면 스킵"이라, 모델이나 차원이 바뀌면 문제가 된다.
    #   JINA_MODEL / JINA_DIMS 를 바꿔 재실행하면 131건이 전부 스킵되어
    #   아무것도 재임베딩되지 않는데 meta.json 만 새 값으로 덮어써진다 →
    #   "새 모델 인덱스"라고 속지만 내용은 옛 벡터인 상태가 조용히 남는다.
    #   그래서 여기서 먼저 막는다.
    if vecs:
        prev = json.loads(META_PATH.read_text(encoding="utf-8")) if META_PATH.exists() else {}
        bad_len = sorted({len(v) for v in vecs.values()} - {DIMS})
        if bad_len:
            sys.exit(
                f"ABORT: 기존 벡터 차원 {bad_len} ≠ 목표 {DIMS}. "
                f"전량 재임베딩이 필요하므로 rm {VEC_PATH} {PROG_PATH} 후 다시 돌릴 것."
            )
        if prev.get("model") and prev["model"] != MODEL:
            sys.exit(
                f"ABORT: 인덱스 모델 불일치 — 기존 '{prev['model']}' vs 현재 '{MODEL}'. "
                f"재개 로직이 기존 벡터를 스킵하므로 재임베딩이 안 일어난다. "
                f"rm {VEC_PATH} {PROG_PATH} 후 다시 돌릴 것 "
                f"(또는 JINA_MODEL={prev['model']} 로 되돌릴 것)."
            )

    todo = [r for r in rows if r["report_id"] not in vecs]
    print(f"[W4] 임베딩 대상 {len(todo)}/{len(rows)} (스킵 {len(rows)-len(todo)})", flush=True)
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        embs = jina_embed([c["text"] for c in chunk], "retrieval.passage")
        for c, e in zip(chunk, embs):
            vecs[c["report_id"]] = e
            prog[c["report_id"]] = True
        VEC_PATH.write_text(json.dumps(vecs, ensure_ascii=False), encoding="utf-8")
        PROG_PATH.write_text(json.dumps(prog, ensure_ascii=False), encoding="utf-8")
        print(f"[W4] {min(i+BATCH, len(todo))}/{len(todo)}", flush=True)
        time.sleep(1)

    meta = {"model": MODEL, "dims": DIMS, "task_doc": "retrieval.passage",
            "task_query": "retrieval.query", "rrf_k": RRF_K,
            "bm25s": bm25s.__version__ if hasattr(bm25s, "__version__") else "?",
            "kiwipiepy": kiwipiepy.__version__, "n_docs": len(rows)}
    META_PATH.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[W4] 완료: vectors {len(vecs)}개, meta={meta}")


if __name__ == "__main__":
    main()
