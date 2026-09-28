"""Worker JS ↔ Python app/tools/search.py 교차 검증.

  1. node parity_worker.mjs 로 mcp/ Worker 를 로컬 구동해 JSON-RPC 응답을 받고
  2. app.tools.search.search_reports 로 같은 쿼리를 돌린 뒤
  3. 후보 report_id 순서 · stage_score · drop 여부 · signals 를 대조한다.

vector_weight=0 (BM25 단독) 으로 고정. Jina 키가 크레딧 소진이라 양쪽 다
vector 경로를 못 쓴다. 그래도 슬롯 파싱·BM25·stage 솔버·카탈로그 전 구간이
검증된다.

  .venv/bin/python pipeline/parity_check.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(API_ROOT))

from app.tools import search  # noqa: E402

QUERIES = [
    "조달업체 면허 업종 등록 현황",
    "쇼핑몰 납품금액과 단가",
    "서울 공공기관 노트북 계약금액",
    "수의계약 실적",
    "입찰공고",
    "지역별 계약 현황",
    "여성기업 입찰",
    "시각화 보고서",
]

HARNESS = Path("/tmp/opencode/parity_worker.mjs")
REF = Path("/tmp/opencode/mcp_ref/worker.js")


def run_worker() -> dict:
    if not HARNESS.exists() or not REF.exists():
        raise SystemExit(
            "레퍼런스 하네스 없음. git show HEAD:mcp/worker.js 로 /tmp/opencode/mcp_ref/ 에 복원할 것."
        )
    out = subprocess.run(
        ["node", str(HARNESS), str(API_ROOT), *QUERIES],
        capture_output=True,
        text=True,
        timeout=300,
        check=True,
    )
    return json.loads(out.stdout)


def norm(d: dict) -> dict:
    """비교용 최소 형태.

    ★ bm25_rank 를 그대로 비교하면 **항상** 어긋난다. 근거:
      - 문서 인덱스는 kiwipiepy + KEEP_TAGS 로 빌드됨 (pipeline/build_hybrid_index.py)
      - Worker 의 쿼리 토크나이저는 정규식 근사 (worker.js qtok(), 주석상 "kiwi 근사")
        예: '시각화' → worker ['시각화'] / kiwipiepy ['시각']
      - 즉 Worker 쪽이 "인덱스 토큰 ≠ 쿼리 토큰" 불일치를 가진 상태였고,
        Python 쪽은 양쪽 모두 kiwipiepy 라 자기일관적이다.
      Worker 주석도 "교착어 미분할은 W7 parity 측정 대상"이라고 인정했다.
      그래서 이 값은 불변식이 아니다 — 슬롯/탈락/후보집합으로 판정한다.
    """

    def cands(x):
        return [
            (
                c["report_id"],
                None if c.get("stage_score") is None else round(float(c["stage_score"]), 3),
                bool(c.get("dropped")),
                round(float((c.get("signals") or {}).get("dims_coverage", 0)), 2),
            )
            for c in x.get("candidates", [])
        ]

    return {
        "cands": cands(d),
        "ids": [c[0] for c in cands(d)],
        "scores": {c[0]: c[1] for c in cands(d)},
        "dropped": {c[0] for c in cands(d) if c[2]},
        "dims": (d.get("query_slots") or {}).get("dims"),
        "family": (d.get("query_slots") or {}).get("family"),
    }


SCORE_TOL = 0.05  # S5 RRF 가산항(4.0 × 1/(K+rank)) 이 bm25_rank 어긋남에 그대로 증폭된다


def main() -> int:
    print("Worker JS 구동 중 (bm25s 인덱스 + Jina 스텁)…")
    ref = run_worker()
    print(f"  {len(ref)}건 수신\n")

    bad = 0
    for q in QUERIES:
        w = norm(ref[q])
        p = norm(search.search_reports(q, top_k=5, vector_weight=0))

        slots_ok = w["dims"] == p["dims"] and w["family"] == p["family"]
        # drop 결정은 토크나이저와 무관하다(S0/S1 는 슬롯 기반). 정확히 같아야 한다.
        drop_ok = w["dropped"] == p["dropped"]
        overlap = len(set(w["ids"]) & set(p["ids"])) / max(1, len(set(w["ids"]) | set(p["ids"])))
        # 같은 report_id 는 점수가 tolerance 안에서 같아야 한다 (순서 무시).
        shared = set(w["scores"]) & set(p["scores"])
        score_ok = all(
            (w["scores"][i] is None and p["scores"][i] is None)
            or (w["scores"][i] is not None and p["scores"][i] is not None
                and abs(w["scores"][i] - p["scores"][i]) <= SCORE_TOL)
            for i in shared
        )
        ok = slots_ok and drop_ok and score_ok and overlap >= 0.6
        bad += not ok

        print(f"{'OK  ' if ok else 'DIFF'}  {q}")
        print(f"        slots={'OK' if slots_ok else 'DIFF'}  "
              f"drop={'OK' if drop_ok else 'DIFF'}  "
              f"score={'OK' if score_ok else 'DIFF'}  "
              f"후보 겹침={overlap:.0%}")
        if not slots_ok:
            print(f"        worker dims={w['dims']} family={w['family']}")
            print(f"        python dims={p['dims']} family={p['family']}")
        if not drop_ok:
            print(f"        worker dropped={sorted(w['dropped'])}")
            print(f"        python dropped={sorted(p['dropped'])}")
        if not score_ok:
            for i in sorted(shared):
                a, b = w["scores"][i], p["scores"][i]
                if a is None or b is None or abs(a - b) > SCORE_TOL:
                    print(f"        {i}: worker={a} python={b}")

    print(f"\n{len(QUERIES) - bad}/{len(QUERIES)} 통과 "
          f"(불변식: 슬롯 · 탈락결정 · 공통후보 점수±{SCORE_TOL} · 후보겹침≥60%)")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
