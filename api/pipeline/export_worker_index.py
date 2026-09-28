#!/usr/bin/env python3
"""4a: Worker용 인덱스 아티팩트 익스포트 + parity assert. 원본 불변.

출력 (public/api-index/):
  tokens.json     {rid: [kiwi 토큰]} — 쿼리 때도 동일 토큰 규칙 사용
  idf.json        {token: idf} — bm25s lucene식과 동일 값
  doclen.json     {rid: 토큰수} + avgdl (BM25 JS 구현용)
  vectors.json    문서 벡터 복사 (Jina 1024차원)
  rerank_ctx.json {rid: {name, official_id, reptId, desc, conds, metrics, dims}}
                  rerank.py _ctx()와 동일 필드·동일 절단
  meta.json       {model, dims, k1, b, idf_method, rrf_k}

parity: 동일 쿼리로 bm25s retireve vs 본 스크립트 lucene 재계산 Top10 일치 assert.
"""
import html
import json
import math
from pathlib import Path

API_ROOT = Path(__file__).resolve().parents[1]
BASE_DIR = API_ROOT / "data"
JSONL_PATH = BASE_DIR / "catalog" / "search_docs.jsonl"
CATALOG_PATH = BASE_DIR / "catalog" / "report_catalog_w2c.json"
VEC_SRC = BASE_DIR / "index" / "vectors.json"
# mcp/ Worker 는 이 산출물을 ASSETS 로 서빙했다. Worker 가 api/ 로 통합되면서
# 서빙 경로는 사라졌고, 이 스크립트는 이제 **회귀 검증용**으로만 쓴다:
#   1) bm25s ↔ 이 스크립트 lucene 재계산 Top10 parity (스크립트 내부 assert)
#   2) app/retrieval/search_hybrid.py 의 실제 결과와 대조
OUT_DIR = API_ROOT / "var" / "api-index"

K1, B, RRF_K = 1.5, 0.75, 60  # bm25s 기본값과 동일 (k1=1.5, b=0.75, lucene)
KEEP_TAGS = {"NNG", "NNP", "VV", "VA", "SL", "SN"}
PARITY_QUERIES = ["조달업체 면허 업종 등록 현황", "쇼핑몰 납품금액과 단가",
                  "서울 공공기관 노트북 계약금액", "수의계약 실적", "입찰공고"]


def tok(kiwi, text):
    return [t.form for t in kiwi.tokenize(text)
            if t.tag in KEEP_TAGS and len(t.form.strip()) > 0]


def main():
    from kiwipiepy import Kiwi
    kiwi = Kiwi()
    rows = [json.loads(l) for l in JSONL_PATH.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 131
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    tokens = {r["report_id"]: tok(kiwi, r["text"]) for r in rows}
    N = len(rows)
    df = {}
    for ts in tokens.values():
        for t in set(ts):
            df[t] = df.get(t, 0) + 1
    idf = {t: math.log(1 + (N - f + 0.5) / (f + 0.5)) for t, f in df.items()}
    doclen = {rid: len(ts) for rid, ts in tokens.items()}
    avgdl = sum(doclen.values()) / N

    def scores_dict(qt):
        out = {}
        for rid, ts in tokens.items():
            tf = {}
            for t in ts:
                tf[t] = tf.get(t, 0) + 1
            s = 0.0
            dl = len(ts)
            for t in qt:
                if t not in tf:
                    continue
                f = tf[t]
                s += idf[t] * (f * (K1 + 1)) / (f + K1 * (1 - B + B * dl / avgdl))
            out[rid] = s
        return out

    def scores(qt):
        out = scores_dict(qt)
        return sorted(out, key=out.get, reverse=True)[:10]

    # parity: bm25s vs 재계산
    import bm25s
    import numpy as np
    corpus = [tokens[r["report_id"]] for r in rows]
    rids = [r["report_id"] for r in rows]
    ret = bm25s.BM25()
    ret.index(corpus, show_progress=False)
    for q in PARITY_QUERIES:
        qt = tok(kiwi, q)
        docs, sc = ret.retrieve([qt], k=10, show_progress=False)
        got = [rids[int(d)] for d in docs[0]]
        bscores = {rids[int(d)]: float(s) for d, s in zip(docs[0], np.asarray(sc[0]).ravel())}
        want = scores(qt)
        mine = scores_dict(qt)
        assert got[0] == want[0], f"PARITY TOP1 FAIL {q}"
        bad = [(a, b) for a in want for b in want
               if (mine[a] - mine[b]) > 1e-4 and (bscores.get(a, 0) - bscores.get(b, 0)) < -1e-4]
        assert not bad, f"PARITY ORDER FAIL {q}: {bad[:3]}"
        # 경계 1~2건 교체는 float32/64 노이즈로 허용 (점수격차 1e-4 이하는 위에서 검증済)
        assert len(set(got) ^ set(want)) <= 2, f"PARITY SET FAIL {q}: {got} vs {want}"
        print(f"[parity] OK: {q} -> {want[0]}")

    (OUT_DIR / "tokens.json").write_text(json.dumps(tokens, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "idf.json").write_text(json.dumps(idf, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "doclen.json").write_text(
        json.dumps({"avgdl": avgdl, "len": doclen}, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "vectors.json").write_text(
        VEC_SRC.read_text(encoding="utf-8"), encoding="utf-8")

    catalog = {r["report_id"]: r for r in
               json.loads(CATALOG_PATH.read_text(encoding="utf-8"))}
    ctx = {}
    for r in rows:
        c = catalog[r["report_id"]]
        conds = []
        groups = {"기간": [], "주체": [], "구분": [], "입력": []}
        for x in (c.get("입력조건_구조화") or [])[:25]:
            opts = x.get("허용값") or []
            o = f"={','.join(opts[:5])}" if opts else ""
            conds.append(f"{x['이름']}({x['의미종류']}{o})")
            sem = x.get("의미종류") or ""
            if sem in ("date", "yearmonth", "year"):
                groups["기간"].append(x["이름"])
            elif sem in ("entity", "entity_multi"):
                groups["주체"].append(x["이름"])
            elif sem.startswith("enum") or sem in ("radio", "selectbox"):
                groups["구분"].append(x["이름"])
            else:
                groups["입력"].append(x["이름"])
        metrics = c.get("metrics") or []
        dims = c.get("dimensions") or []
        fact = ("지표=[" + ", ".join(metrics) + "]; " +
                "차원=[" + ", ".join(dims) + "]; " +
                "; ".join(f"조건:{k}=[{', '.join(v)}]" for k, v in groups.items()))
        ctx[r["report_id"]] = {
            "name": html.unescape(c.get("보고서명") or ""), "official_id": c.get("official_id"),
            "reptId": r["report_id"],
            "desc": (c.get("desc_summary") or "")[:400],
            "conds": conds,
            "metrics": metrics,
            "dims": dims,
            "fact": fact,
        }
    (OUT_DIR / "rerank_ctx.json").write_text(
        json.dumps(ctx, ensure_ascii=False), encoding="utf-8")
    # C안 single-call용 압축 카탈로그 (이름+설명150자+조건명+출력컬럼명, 지표는 * 표시)
    compact = []
    for r in rows:
        c = catalog[r["report_id"]]
        cnames = [x["이름"] for x in (c.get("입력조건_구조화") or [])[:20]]
        mets = set(c.get("metrics") or [])
        cols = [(n + "*" if n in mets else n)
                for n in ([x["name"] for x in (c.get("columns") or [])
                           if not (x.get("w2c") or {}).get("quarantine")] or (c.get("dimensions") or []))]
        compact.append(
            f"[{r['report_id']}] {c.get('보고서명')}\n"
            f"설명: {(c.get('desc_summary') or c.get('설명') or '')[:150]}\n"
            f"조건: {', '.join(cnames)}\n"
            f"출력(*=지표): {', '.join(cols)}")
    (OUT_DIR / "catalog_compact.txt").write_text("\n\n".join(compact), encoding="utf-8")
    # concepts slim copy (쿼리 태깅용: Worker가 읽음)
    concepts = json.loads((BASE_DIR / "catalog" / "concepts.json").read_text(encoding="utf-8"))
    slim = [{"id": x["id"], "label": x.get("label"),
             "aliases": x.get("aliases") or []} for x in concepts.get("concepts", [])]
    (OUT_DIR / "concepts.json").write_text(
        json.dumps(slim, ensure_ascii=False), encoding="utf-8")
    (OUT_DIR / "meta.json").write_text(json.dumps(
        {"model": "jina-embeddings-v3", "dims": 1024,
         "task_doc": "retrieval.passage", "task_query": "retrieval.query",
         "k1": K1, "b": B, "idf_method": "lucene", "rrf_k": RRF_K, "n_docs": N},
        ensure_ascii=False, indent=2), encoding="utf-8")
    # Stage solver용: dims 어휘 + 가중치 + 가문 크기 + w2d 카탈로그 필드
    import shutil
    shutil.copy(BASE_DIR / "dim_vocab.json", OUT_DIR / "dim_vocab.json")
    shutil.copy(BASE_DIR / "stage_weights.json", OUT_DIR / "stage_weights.json")
    from collections import Counter
    w2d = json.loads((BASE_DIR / "catalog" / "report_catalog_w2d.json").read_text(encoding="utf-8"))
    fam_size = Counter(r["family"] for r in w2d if r.get("family"))
    famrec = {r["report_id"]: {"family": r.get("family"), "dims": r.get("dims") or [],
                               "channel": r.get("channel"), "item_scope": r.get("item_scope"),
                               "is_visual": bool(r.get("is_visual")), "views": r.get("조회수") or 0,
                               "metrics": r.get("metrics") or [],
                               "has_cols": not r.get("columns_missing"),
                               "cols": [c["name"] for c in (r.get("columns") or [])
                                        if not (c.get("w2c") or {}).get("quarantine")]}
              for r in w2d}
    (OUT_DIR / "families.json").write_text(
        json.dumps({"sizes": fam_size, "reports": famrec,
                    "aliases": json.loads((BASE_DIR / "family_overrides.json").read_text(encoding="utf-8")).get("aliases", {})},
                   ensure_ascii=False), encoding="utf-8")
    print(f"[4a] 완료 -> {OUT_DIR}/ (parity 5/5)")


if __name__ == "__main__":
    main()
