"""로컬 ONNX 임베더 — Jina API 대체 경로.

왜 로컬인가: Jina API 가 403(crzadit 소진)으로 죽었다. 검색의 vector 절반을
대체할 수단이 필요했다.

모델 선택 기준 (이 박스 실측 — aarch64 4코어, 22G, Onyx 10컨테이너 동시 가동):
  - e5-small (intfloat/multilingual-e5-small, ONNX, MIT) 채택. 384d, 로드 2초,
    쿼리 33ms, RSS +386MB.
  - jina-embeddings-v3 fp16(1.1G) / bge-m3 fp32(2.2G + 외부 2.2G) 는 이 박스에서
    로드하는 순간 OOM 으로整机 죽었다(실측). 리소스로 못 버틴다.
  - v5-text-small 은 공식 ONNX 가 없고 커뮤니티 변환물(3회 다운로드, 라이선스
    미선언) 뿐이다. 채택하지 않는다.

의존: onnxruntime + tokenizers. torch 불필요 → trust_remote_code 도 불필요
(가중치만 받고 코드는 실행하지 않는다).

간결함: SentenceTransformer API 를 흉내내지 않는다. 필요한 연산만 노출한다 —
앱은 이 모듈을 직접 호출하고 파이프라인(BM25/RRF/stage)과 붙이는 것만 하면 된다.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "2")
os.environ.setdefault("OMP_WAIT_POLICY", "PASSIVE")

import numpy as np
import onnxruntime as ort
from huggingface_hub import hf_hub_download, snapshot_download
from tokenizers import Tokenizer

DEFAULT_REPO = "intfloat/multilingual-e5-small"
DEFAULT_ONNX = "onnx/model.onnx"
MAX_TOKENS = 512
# e5 계열은 query/passage 접두사로 비대칭 검색을 표현한다. 이걸 빼면 품질이 떨어진다.
QUERY_PREFIX = "query: "
PASSAGE_PREFIX = "passage: "


class LocalEmbedder:
    """문장 리스트 → 정규화된 (n, dim) float32."""

    def __init__(
        self,
        repo: str = DEFAULT_REPO,
        onnx_file: str = DEFAULT_ONNX,
        threads: int = 2,
        max_tokens: int = MAX_TOKENS,
    ) -> None:
        self.repo = repo
        self.onnx_file = onnx_file
        self.threads = threads
        self.max_tokens = max_tokens
        self._sess: ort.InferenceSession | None = None
        self._tok: Tokenizer | None = None
        self._input_names: set[str] = set()
        self._lock = threading.Lock()

    # ---- 로딩 ----

    def load(self) -> None:
        """모델을 메모리에 올린다. 스레드 안전(앱 기동 warmup 에서 호출될 수 있음)."""
        with self._lock:
            if self._sess is not None:
                return
            so = ort.SessionOptions()
            # 이 박스는 4코어 전부다. 4개를 이 모델에 고정하면
            # Next.js·FastAPI·Onyx 가 밀린다. 2가 실용적 선이다.
            so.intra_op_num_threads = self.threads
            so.inter_op_num_threads = 1
            so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            # 1GB 이상 모델은 가중치가 .onnx_data 로 분리된다. 그래프만 받으면
            # "External data path validation failed" 로 실패한다 → onnx/ 디렉터리
            # 전체를 한 로컬 경로로 받아야 한다.
            local = Path(
                snapshot_download(self.repo, allow_patterns=["onnx/*", "tokenizer.json"])
            )
            sess = ort.InferenceSession(
                str(local / self.onnx_file), so, providers=["CPUExecutionProvider"]
            )
            tok = Tokenizer.from_file(hf_hub_download(self.repo, "tokenizer.json"))
            tok.enable_truncation(max_length=self.max_tokens)
            tok.enable_padding(pad_id=0, pad_token="[PAD]")
            self._sess, self._tok = sess, tok
            self._input_names = {i.name for i in sess.get_inputs()}

    @property
    def loaded(self) -> bool:
        return self._sess is not None

    # ---- 임베딩 ----

    def embed(self, texts: list[str], batch: int = 8) -> np.ndarray:
        if self._sess is None:
            self.load()
        assert self._sess is not None and self._tok is not None
        out: list[np.ndarray] = []
        for i in range(0, len(texts), batch):
            encs = self._tok.encode_batch(texts[i : i + batch])
            ids = np.array([e.ids for e in encs], dtype=np.int64)
            mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
            feed: dict[str, np.ndarray] = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self._input_names:
                feed["token_type_ids"] = np.zeros_like(ids)
            o = self._sess.run(None, {k: v for k, v in feed.items() if k in self._input_names})
            h = o[0]
            # e5 = mean pooling
            m = mask[..., None].astype(np.float32)
            out.append((h * m).sum(axis=1) / np.clip(m.sum(axis=1), 1e-9, None))
        v = np.vstack(out).astype(np.float32)
        # 코사인 유사도를 쓰므로 정규화해서 반환한다 — 호출부가 매번 정규화할
        # 필요가 없도록.
        return v / np.clip(np.linalg.norm(v, axis=1, keepdims=True), 1e-9, None)

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed([QUERY_PREFIX + text])[0]

    def embed_passages(self, texts: list[str], batch: int = 8) -> np.ndarray:
        return self.embed([PASSAGE_PREFIX + t for t in texts], batch=batch)


_default: LocalEmbedder | None = None


def get_default() -> LocalEmbedder:
    """프로세스 공통 인스턴스.

    검색마다 `LocalEmbedder()` 를 새로 만들면 load() 가 매번 처음부터 돌아
    세션(ORT graph 최적화 포함)을 다시 만들어 1.9초를 날린다. 앱 프로세스당
    하나만 쓴다 — RSS +385MB.
    """
    global _default
    if _default is None:
        _default = LocalEmbedder()
    return _default


def warmup() -> str:
    """기동 시 미리 로드. 실패해도 치명적이지 않다(질의 시 lazy)."""
    try:
        get_default().load()
        return "ok"
    except Exception as e:  # noqa: BLE001
        return f"실패: {type(e).__name__}: {e}"
