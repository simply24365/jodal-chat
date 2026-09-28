#!/usr/bin/env python3
"""쿼리 개념 태깅 (결정적, LLM 불사용). concepts.json aliases substring 매치.

  from app.retrieval.concept_tagger import tag_query, expand_query
  tag_query("발주기관 기준 계약 현황") -> [org_client 개념, ...]
  expand_query(q) -> 원문 + 매칭 개념의 label/aliases (BM25 쿼리 확장용, Vector는 원문 유지)
"""
import json
from functools import lru_cache
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[2]
BASE_DIR = API_ROOT / "data"
CONCEPTS_PATH = BASE_DIR / "catalog" / "concepts.json"


@lru_cache(maxsize=1)
def _concepts():
    return json.loads(CONCEPTS_PATH.read_text(encoding="utf-8")).get("concepts", [])


def _norm(s):
    return "".join(str(s or "").split())


def tag_query(q):
    """매칭 개념 리스트 (label/alias가 정규화 쿼리의 부분문자열이면 태그)."""
    nq = _norm(q)
    out = []
    for c in _concepts():
        terms = [c.get("label")] + list(c.get("aliases") or [])
        if any(t and _norm(t) in nq for t in terms if t):
            out.append(c)
    return out


def expand_query(q):
    """원문 + 태그된 개념의 label/aliases (중복 제거, 순서 유지)."""
    extra = []
    for c in tag_query(q):
        for t in [c.get("label")] + list(c.get("aliases") or []):
            if t and t not in extra:
                extra.append(t)
    return (q + " " + " ".join(extra)).strip() if extra else q
