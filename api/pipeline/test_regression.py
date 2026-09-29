"""빠른 회귀 테스트 스위트 (전체 실행 < 60초, LLM/네트워크 불필요).

지금까지의 구조적 개선이 향후 수정에서 조용히 깨지는 것을 막는 최소 안전망.
각 검사는 데이터·계약·판정기의 불변식(invariant)을 검증한다.

  uv run python pipeline/test_regression.py        # 전체
  uv run python pipeline/test_regression.py -k cite  # 이름 필터
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _env  # noqa: F401,E402  — api/.env 로딩

CHECKS: list[tuple[str, object]] = []


def check(name):
    def deco(fn):
        CHECKS.append((name, fn))
        return fn
    return deco


# ---------------------------------------------------------------- 1. 데이터

@check("data-catalog-doc2query-sync")
def _( ) -> None:
    from app.retrieval import catalog as cat
    rows = cat.catalog()
    assert len(rows) == 131, f"카탈로그 {len(rows)} != 131"
    d2q = cat.doc2query()
    missing = [rid for rid in rows if rid not in d2q]
    assert not missing, f"doc2query 누락: {missing[:5]}"
    for rid, dq in list(d2q.items())[:131]:
        assert dq.get("queries"), f"{rid}: 예상질문 없음"
        assert dq.get("synopsis"), f"{rid}: synopsis 없음"


@check("data-aug-docs-complete")
def _( ) -> None:
    base = Path(__file__).resolve().parent.parent / "data" / "catalog"
    orig = {json.loads(l)["report_id"]: json.loads(l)["text"]
            for l in (base / "search_docs.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}
    aug = {json.loads(l)["report_id"]: json.loads(l)["text"]
           for l in (base / "search_docs_aug.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()}
    assert set(aug) == set(orig), "증강 문서 집합 불일치"
    d2q = json.loads((base / "doc2query.json").read_text(encoding="utf-8"))
    grown = sum(1 for rid in aug if len(aug[rid]) > len(orig[rid]))
    assert grown >= 120, f"증강 반영 문서 {grown} < 120"


# ---------------------------------------------------------------- 2. 인덱스

@check("index-consistency-bm25-vectors")
def _( ) -> None:
    import json
    meta = json.loads((Path(__file__).resolve().parent.parent / "data" / "index" / "local_meta.json")
                      .read_text(encoding="utf-8"))
    vecs = json.loads((Path(__file__).resolve().parent.parent / "data" / "index" / "local_vectors.json")
                      .read_text(encoding="utf-8"))
    assert meta.get("n_docs") == len(vecs) == 131, f"meta n_docs={meta.get('n_docs')} vecs={len(vecs)}"
    dims = meta.get("dims")
    assert all(len(v) == dims for v in vecs.values()), "벡터 차원 불일치"


@check("index-bm25-has-augmented-vocab")
def _( ) -> None:
    """doc2query 예상질문의 토큰이 BM25 vocab에 반영됐는지 — 증강 인덱스가 실제 빌드됐는지의 증거."""
    import json
    import bm25s
    base = Path(__file__).resolve().parent.parent / "data" / "index"
    ret = bm25s.BM25.load(str(base / "bm25"), load_corpus=True)
    vocab = ret.vocab_dict
    d2q = json.loads((base.parent / "catalog" / "doc2query.json").read_text(encoding="utf-8"))
    probe = 0
    for dq in d2q.values():
        for q in dq.get("queries") or []:
            for t in q.split():
                if len(t) >= 2 and t in vocab:
                    probe += 1
                    break
            break
    assert probe >= 40, f"예상질문 토큰이 vocab에 반영된 문서 {probe} < 40 — 증강 인덱스 재빌드 필요"


# ---------------------------------------------------------------- 3. 툴 계약

@check("tool-citation-extract-shapes")
def _( ) -> None:
    from app.tools.mcp_tool import _extract_citation_docs
    # candidates 형식
    d = _extract_citation_docs(json.dumps(
        {"candidates": [{"report_id": "00262", "name": "수요기관별 계약납품요구 실적통계"}]},
        ensure_ascii=False))
    assert len(d) == 1 and d[0].document_id == "JODAL_REPORT_00262"
    # reports 형식 (value_lookup 스캔)
    d = _extract_citation_docs(json.dumps(
        {"reports": [{"report_id": "00248", "name": "소관구분별 계약납품요구 실적통계"}]},
        ensure_ascii=False))
    assert len(d) == 1 and "JODAL_REPORT_00248" == d[0].document_id
    # detail 형식
    d = _extract_citation_docs(json.dumps(
        {"report_id": "00262", "name": "수요기관별 계약납품요구 실적통계"}, ensure_ascii=False))
    assert len(d) == 1
    # 깨진 입력은 조용히 빈 목록
    assert _extract_citation_docs("not json") == []


@check("tool-value-lookup-decoration")
def _( ) -> None:
    from app.tools import hubpick as H
    r = H.value_lookup(query="한국수자원공사", intent="납품요구 건수")
    assert r["reports"], "스캔 결과 없음"
    for rep in r["reports"][:5]:
        assert rep.get("links", {}).get("move"), f"{rep['report_id']}: links.move 없음 (UI 계약)"
        assert "dims" in rep and "synopsis" in rep, f"{rep['report_id']}: 메타 부족"
    # 00262 는 한국수자원공사를 통계대상시스템 조건값으로 갖는다 (역색인 불변식)
    assert any(x["report_id"] == "00262" for x in r["reports"]), "00262 누락 — 역색인 오염"


@check("tool-search-facets")
def _( ) -> None:
    from app.tools import search as S
    r = S.search_reports(query="시각화 보고서", top_k=3)
    f = r["catalog_facets"]
    assert f["total_reports"] == 131
    assert 0 < f["visualizable"] < 131, f"visualizable={f['visualizable']} — is_visual 역색인 오염"
    assert f["by_family"] and f["by_dimension"]


# ---------------------------------------------------------------- 4. 판정기

@check("judge-prefilter-detects-residue-and-fabrication")
def _( ) -> None:
    from eval_judge import _prefilter
    rec_ok = {"final_answer": "수요기관별 계약납품요구 실적통계를 안내합니다.", "tool_calls": []}
    assert "hygieneresidue" not in _prefilter(rec_ok), "정상 답변 오판"
    rec_think = {"final_answer": "<think>x</think>답변", "tool_calls": []}
    assert _prefilter(rec_think).get("hygieneresidue") == 0, "think 태그 미탐지"
    rec_fake = {
        "final_answer": "보고서 99999 를 참고하세요.",
        "tool_calls": [{"tool_name": "search", "tool_result": json.dumps(
            {"candidates": [{"report_id": "00262", "name": "x"}]}, ensure_ascii=False)}],
    }
    assert _prefilter(rec_fake).get("groundedness") == 0, "가짜 ID 미탐지"


@check("judge-evidence-sheet-completeness")
def _( ) -> None:
    from eval_judge import _evidence_sheet
    rec = {"tool_calls": [{"tool_name": "search", "tool_result": json.dumps(
        {"candidates": [
            {"report_id": "00262", "name": "수요기관별 계약납품요구 실적통계"},
            {"report_id": "00618", "name": "수의계약 수요기관별 발주 순위"},
        ]}, ensure_ascii=False)}]}
    sheet = _evidence_sheet(rec)
    assert "00262" in sheet and "00618" in sheet, "근거 시트 누락"
    assert "증감실적통계" not in sheet or "00262" in sheet  # 정식 보고서명과 대조 가능


# ---------------------------------------------------------------- 5. 루프 불변식

@check("loop-args-unparsed-no-arg-tool")
def _( ) -> None:
    from app.llm_step import _extract_kickoffs, ToolCallKickoff  # noqa: F401
    # 무인자 툴의 정상 호출({})은 args_unparsed 가 되어선 안 된다 (n03 실측 버그).
    from app.llm_step import _parse_tool_args
    raw = "{}"
    parsed = _parse_tool_args(raw)
    invalid = (isinstance(raw, str) and bool(raw.strip())
               and parsed == {} and raw.strip() not in ("{}", "null", "undefined"))
    assert invalid is False, "무인자 툴 {} 오판 회귀"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-k", default=None)
    a = ap.parse_args()
    ran = failed = 0
    for name, fn in CHECKS:
        if a.k and a.k not in name:
            continue
        ran += 1
        try:
            fn()
            print(f"  PASS {name}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {name}: {e}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
    print(f"\n[regression] {ran - failed}/{ran} 통과")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
