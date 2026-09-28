"""Jina 임베딩 클라이언트.检索(BM25 hybrid) 쿼리 임베딩용.

원본: pipeline/build_hybrid_index.py 의 jina_embed() 를 런타임으로 이식.
파이프라인이 앱을 참조하지 않도록(그리고 앱이 파이프라인을 참조하지 않도록)
양쪽 다 이 모듈을 쓰도록 통합했다.

인덱스 빌드(pipeline)와 쿼리(app)는 같은 모델/차원을 공유해야 하므로
MODEL/DIMS 는 index/meta.json 을 Single Source of Truth 로 삼는다.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
META_PATH = API_ROOT / "data" / "index" / "meta.json"

ENDPOINT = "https://api.jina.ai/v1/embeddings"
FALLBACK_MODEL = "jina-embeddings-v3"
FALLBACK_DIMS = 1024
FALLBACK_TASK_QUERY = "retrieval.query"

_meta: dict | None = None


def _load_meta() -> dict:
    """index/meta.json 에서 model/dims/task_query 를 읽는다(인덱스 불일치 방지)."""
    global _meta
    if _meta is None:
        try:
            _meta = json.loads(META_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _meta = {}
    return _meta


def model() -> str:
    return os.environ.get("JINA_MODEL") or _load_meta().get("model") or FALLBACK_MODEL


def dims() -> int:
    raw = os.environ.get("JINA_DIMS")
    if raw:
        try:
            return int(raw)
        except ValueError:
            pass
    return int(_load_meta().get("dims") or FALLBACK_DIMS)


def task_query() -> str:
    return _load_meta().get("task_query") or FALLBACK_TASK_QUERY


class EmbeddingError(RuntimeError):
    """임베딩 실패. 툴 러너가 사용자 안내 문구로 변환한다."""

    def __init__(self, msg: str, *, permanent: bool = False):
        super().__init__(msg)
        # permanent=True 는 재시도해도 소용없는 실패(인증/크레딧/잘못된 요청).
        # 호출부가 circuit breaker 를 닫을 때 쓴다.
        self.permanent = permanent


# 이 HTTP 상태는 재시도해도 안 된다. 특히 402/403 은 크레딧 소진/키 폐기라
# 백오프로 3회 재시도하면 호출 하나가 30초를 태워 버린다(실측).
_PERMANENT_CODES = {400, 401, 402, 403, 404, 422}

# 연속 영구 실패를 기억한다. Jina 크레딧이 죽은 채로 매 요청마다 3회씩
# 재시도하면 사용자가 그만큼 기다리므로, 이후엔 즉시 강등시킨다.
# (키를 다시 채우면 TTL 이 지나면 자동 복구된다)
_breaker_until: float = 0.0
_BREAKER_TTL_SEC = 300.0


def breaker_open() -> bool:
    return time.monotonic() < _breaker_until


def _trip_breaker() -> None:
    global _breaker_until
    _breaker_until = time.monotonic() + _BREAKER_TTL_SEC


def embed(texts: list[str], task: str | None = None) -> list[list[float]]:
    """Jina embeddings 호출. 실패 시 EmbeddingError.

    주의: 원본 jina_embed() 는 sys.exit() 로 프로세스를 죽였다. 앱 안에서 쓰면
    uvicorn 워커가 죽으므로 예외로 바꾼다.
    """
    if breaker_open():
        raise EmbeddingError(
            "Jina 임베딩 일시 중단(직전 영구 실패). BM25 단독으로 처리한다.",
            permanent=True,
        )
    key = os.environ.get("JINA_API_KEY", "")
    if not key:
        raise EmbeddingError("JINA_API_KEY 미설정", permanent=True)
    body = json.dumps(
        {
            "model": model(),
            "task": task or task_query(),
            "dimensions": dims(),
            "input": texts,
        }
    ).encode()
    last = ""
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                ENDPOINT,
                data=body,
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0",
                },
            )
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
                d = json.loads(r.read().decode())
            return [e["embedding"] for e in d["data"]]
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code} {e.read()[:150]!r}"
            if e.code in _PERMANENT_CODES:
                _trip_breaker()
                raise EmbeddingError(f"Jina 임베딩 실패: {last}", permanent=True) from e
            time.sleep(2 * (attempt + 1))  # 429/5xx 만 짧게 재시도
        except Exception as e:  # noqa: BLE001
            last = str(e)[:150]
            time.sleep(2 * (attempt + 1))
    raise EmbeddingError(f"Jina 임베딩 실패: {last}")
