"""조달 통계 카탈로그 로더 (131건).

Single Source of Truth: data/catalog/report_catalog_w2d.json
이 파일 하나가 mcp/ Worker 가 여러 개로 쪼개 쓰던
`rerank_ctx.json`(이름·조건·지표) + `families.json`(family·dims·조회수)을 모두 포함한다.
따라서 Worker 전용 아티팩트를 따로 만들 필요가 없고, idf/tokens 벡터 등
검색 인덱스도 이 카탈로그와 어긋나지 않는다.

public_cond_name() 은 mcp/ 의 publicCondName() 이식 — 조건 문자열 끝에 붙은
UI 타입 토큰(yearmonth·enum_multi=…)을 떼어내 LLM 이 사람용 설명에 섞지 않게 한다.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

API_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = API_ROOT / "data"
CATALOG_PATH = DATA_DIR / "catalog" / "report_catalog_w2d.json"
CONCEPTS_PATH = DATA_DIR / "catalog" / "concepts.json"
DOC2QUERY_PATH = DATA_DIR / "catalog" / "doc2query.json"

DATA_BASE = "https://data.g2b.go.kr/link/AISC001_01/"
# mcp/ 의 PROXY(https://g2bgo.simply24365.workers.dev/). 데이터가 아니라
# "이 보고서를 허브 웹에서 열어줘" 딥링크 생성기였으므로 같은 형태의 URL 로 대체한다.
# 원 Worker 는 g2bOpen=<rid> 로 report_id 를 넘겼다.
G2B_OPEN_BASE = "https://data.g2b.go.kr/link/AISC001_01/"

CATALOG_VER = "w2d-20260919|jina-embeddings-v3-1024"

# "마감년월(yearmonth)" → "마감년월". From/To 는 보존: "납품요구일자(From)_전일(date)" →
# "납품요구일자(From)_전일"
_COND_UI_TAIL_RE = re.compile(r"\(([a-z][a-z0-9_]*(?:=.*)?)\)\s*$")

_catalog: dict[str, dict[str, Any]] | None = None
_concepts: list[dict[str, Any]] | None = None
_doc2query: dict[str, dict[str, Any]] | None = None


def catalog() -> dict[str, dict[str, Any]]:
    """report_id → 레코드. 메모리 상주(1.8M 미만)."""
    global _catalog
    if _catalog is None:
        rows = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        _catalog = {str(r["report_id"]): r for r in rows}
    return _catalog


def doc2query() -> dict[str, dict[str, Any]]:
    """report_id → {queries, aliases, synopsis}. pipeline/build_doc2query.py 산출물.

    LLM 생성 데이터 — 검색 후보에 '이 보고서가 어떤 질문에 답하는가'를 직접
    노출해 에이전트의 후보 판단 근거를 늘린다 (프롬프트가 아니라 데이터로).
    """
    global _doc2query
    if _doc2query is None:
        try:
            _doc2query = json.loads(DOC2QUERY_PATH.read_text(encoding="utf-8"))
        except FileNotFoundError:
            _doc2query = {}
    return _doc2query


def concepts() -> list[dict[str, Any]]:
    global _concepts
    if _concepts is None:
        raw = json.loads(CONCEPTS_PATH.read_text(encoding="utf-8"))
        _concepts = raw.get("concepts", [])
    return _concepts


def public_cond_name(raw: Any) -> str:
    """'통계대상시스템(enum_multi=나라장터(중앙조달),…)' → '통계대상시스템'."""
    name = str(raw if raw is not None else "").strip()
    cleaned = _COND_UI_TAIL_RE.sub("", name).strip()
    return cleaned or name


def cond_names(rec: dict[str, Any], limit: int = 25) -> list[str]:
    """입력조건_구조화 → 사용자 노출용 조건명 목록."""
    out = []
    for c in (rec.get("입력조건_구조화") or [])[:limit]:
        nm = c.get("이름")
        if nm:
            out.append(public_cond_name(nm))
    return out


def column_names(rec: dict[str, Any]) -> list[str]:
    return [c.get("name", "") for c in (rec.get("columns") or []) if c.get("name")]


def report_link(official_id: str) -> str:
    """mcp/ 의 reportLink() 이식 — DATA + ?reptNm=<official_id>."""
    from urllib.parse import quote

    return f"{DATA_BASE}?reptNm={quote(str(official_id), safe='')}"


def direct_link(report_id: str) -> str:
    """허브 웹에서 해당 보고서를 여는 딥링크.

    mcp/ 는 Worker PROXY(g2bgo.simply24365.workers.dev/?g2bOpen=<rid>) 를 썼으나
    공개 URL 폐기로 같은 파라미터 규약을 허브 도메인으로 되돌린다.
    """
    from urllib.parse import quote

    return f"{G2B_OPEN_BASE}?g2bOpen={quote(str(report_id), safe='')}"


def _concept_hits(cols: list[str]) -> list[str]:
    """컬럼명이 개념 라벨/alias 와 겹치는 개념 ID 목록.

    mcp/ 의 mcpSignals().concept_hits 규칙 이식:
    terms 어떤 것이 비어있지 않고, 어떤 컬럼명 n 이 t 를 포함하거나 t 가 n 을 포함.
    """
    out = []
    for c in concepts():
        terms = [c.get("label"), *(c.get("aliases") or [])]
        if any(
            t and any(n and (t in n or n in t) for n in cols) for t in terms
        ):
            out.append(c["id"])
    return out


def matching_concept_ids(rec: dict[str, Any]) -> list[str]:
    return _concept_hits(column_names(rec))
