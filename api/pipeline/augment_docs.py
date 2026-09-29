"""w3_docs 보강: doc2query.json + details131 별칭(구 보고서명)을 검색 문서에 병기.

기존 search_docs.jsonl 을 다시 만들지 않고 **증강만** 적용한다 (원본 불변 원칙 유지):
  - data/catalog/search_docs.jsonl           (기존, 불변)
  - data/catalog/search_docs_aug.jsonl       (증강본 — 인덱스 재빌드가 이걸 읽는다)

증강 섹션:
  ## 예상질문  — doc2query.queries (doc2query / InPars 계열 de facto)
  ## 별칭      — doc2query.aliases (기관명 축약형, 예: 한국수자원공사 = 수자원공사)
  ## 요약      — doc2query.synopsis (질의 관점 요약)
  ## 구보고서명 — details131 desc 의 ASIS 보고서명 (원문에만 있던 별칭)

  uv run python pipeline/augment_docs.py [--source search_docs.jsonl] [--out search_docs_aug.jsonl]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

API = Path(__file__).resolve().parent.parent
CAT = API / "data" / "catalog"
D2Q = CAT / "doc2query.json"
DETAILS = API / "data" / "details131.json"

ASIS_RE = re.compile(r"ASIS\s*보고서명\s*\n?(.+?)(?:\n※|\n\n|$)", re.DOTALL)


def asis_alias(desc: str) -> str:
    m = ASIS_RE.search(desc or "")
    return m.group(1).strip().replace("\n", " / ") if m else ""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="search_docs.jsonl")
    ap.add_argument("--out", default="search_docs_aug.jsonl")
    ap.add_argument("--require-d2q", action="store_true",
                    help="doc2query.json 이 없으면 실패 (완전 생성 후 재빌드용)")
    a = ap.parse_args()

    d2q = json.loads(D2Q.read_text(encoding="utf-8")) if D2Q.exists() else {}
    if a.require_d2q and not d2q:
        raise SystemExit(f"doc2query.json 없음 — 먼저 build_doc2query.py 실행")
    details = json.loads(DETAILS.read_text(encoding="utf-8")) if DETAILS.exists() else {}

    rows = [json.loads(l) for l in (CAT / a.source).read_text(encoding="utf-8").splitlines() if l.strip()]
    n_q = n_a = n_asis = 0
    out_rows = []
    for row in rows:
        rid, text = row["report_id"], row["text"]
        extra = []
        dq = d2q.get(rid) or {}
        qs = dq.get("queries") or []
        if qs:
            extra.append("## 예상질문\n" + "\n".join(f"- {q}" for q in qs))
            n_q += 1
        al = dq.get("aliases") or {}
        if al:
            extra.append("## 값별칭\n" + "\n".join(f"- {k} = {v}" for k, v in al.items()))
            n_a += 1
        syn = dq.get("synopsis")
        if syn:
            extra.append(f"## 한줄요약\n{syn}")
        asis = asis_alias((details.get(rid) or {}).get("desc") or "")
        if asis and asis not in text:
            extra.append(f"## 구보고서명\n{asis}")
            n_asis += 1
        new_text = text.rstrip() + ("\n\n" + "\n\n".join(extra) + "\n" if extra else "\n")
        out_rows.append({"report_id": rid, "text": new_text})

    out = CAT / a.out
    out.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in out_rows), encoding="utf-8")
    avg = sum(len(r["text"]) for r in out_rows) // len(out_rows)
    print(f"[augment] 문서 {len(out_rows)} → {out}")
    print(f"  예상질문 {n_q} · 값별칭 {n_a} · 구보고서명 {n_asis} · 평균 {avg}자")


if __name__ == "__main__":
    main()
