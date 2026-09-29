"""로컬 임베딩 인덱스 빌드 — e5-small(ONNX) → data/index/local_vectors.json

Jina v3 벡터(data/index/vectors.json)는 **덮어쓰지 않는다.** 그건 Jina 크레딧이
있을 때만 재생성 가능하고, 지금은 403 이다. 로컬 벡터는 별도 파일에 쓴다.

  uv run python pipeline/build_local_index.py

산출물:
  data/index/local_vectors.json  {rid: [384 floats]}  (정규화됨)
  data/index/local_meta.json     {model, dims, onnx_file, n_docs, built_at}
"""

from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

API = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API))
sys.path.insert(0, str(API / "app" / "retrieval"))

from app.retrieval.onnx_embed import LocalEmbedder  # noqa: E402

import argparse

JSONL_DEFAULT = API / "data" / "catalog" / "search_docs.jsonl"
_AUG = API / "data" / "catalog" / "search_docs_aug.jsonl"
JSONL = _AUG if _AUG.exists() else JSONL_DEFAULT  # 증강본 우선 (doc2query 병기)
IDX = API / "data" / "index"
OUT_VEC = IDX / "local_vectors.json"
OUT_META = IDX / "local_meta.json"
BATCH = 8


def main() -> int:
    rows = [
        json.loads(x)
        for x in JSONL.read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]
    rids = [r["report_id"] for r in rows]
    assert len(rids) == 131, f"문서 {len(rids)} != 131"

    m = LocalEmbedder()
    t0 = time.time()
    m.load()
    print(f"[local] 모델 로드 {time.time() - t0:.1f}s ({m.repo})", flush=True)

    t0 = time.time()
    dv = m.embed_passages([r["text"] for r in rows], batch=BATCH)
    print(f"[local] {len(rows)}건 임베딩 {time.time() - t0:.1f}s · dim {dv.shape[1]}", flush=True)

    vecs = {rid: [round(float(x), 6) for x in dv[i]] for i, rid in enumerate(rids)}
    OUT_VEC.write_text(json.dumps(vecs, ensure_ascii=False), encoding="utf-8")
    OUT_META.write_text(
        json.dumps(
            {
                "model": m.repo,
                "dims": int(dv.shape[1]),
                "onnx_file": m.onnx_file,
                "passage_prefix": "passage: ",
                "query_prefix": "query: ",
                "n_docs": len(rids),
                "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[local] 완료 -> {OUT_VEC} ({OUT_VEC.stat().st_size // 1024}KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
