#!/usr/bin/env python3
"""W2a: 설명 정제 (규칙 100% + LLM 요약, 원본 불변·재개 지원).

입력(읽기만): data/catalog/report_catalog.json (W1 산출)
출력: data/catalog/report_catalog_w2a.json (W1 복사 + desc_clean/desc_summary 추가)
진행: data/catalog/w2a_progress.json — 성공분 스킵, LLM 호출은 llm_chain(레이트리밋+폴백)

규칙 정제: 설명+desc 합치기 → HTML엔티티 디코드 → 공백 정규화 → 중복문장 제거.
LLM 요약: 프롬프트 v1 고정, 원문(desc_clean) 병존. 실패 시 summary=null로 기록 후 계속.

사용:
  python3 tools/w2a_refine.py --limit 3        # 맛보기
  python3 tools/w2a_refine.py                  # 전체 (성공분 스킵)
  python3 tools/w2a_refine.py --force          # LLM 요약만 재실행
  python3 tools/w2a_refine.py --rules-only     # LLM 없이 규칙만
"""
import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from llm_chain import chat, LLMExhausted  # noqa: E402

BASE_DIR = Path(__file__).resolve().parent.parent
IN_PATH = BASE_DIR / "data" / "catalog" / "report_catalog.json"
OUT_PATH = BASE_DIR / "data" / "catalog" / "report_catalog_w2a.json"
PROG_PATH = BASE_DIR / "data" / "catalog" / "w2a_progress.json"

PROMPT_V1 = ("다음은 조달데이터허브 보고서의 설명문이다. 핵심 내용만 2~3문장으로 요약하라. "
             "없는 사실을 지어내지 말고, 보고서가 '무엇을 조회하는지'와 '추출 기준'이 있으면 포함하라.\n\n{desc}")


def rule_clean(설명, desc):
    raw = " ".join(t for t in [설명 or "", desc or ""] if t.strip())
    text = html.unescape(raw)                    # &#40; 등
    text = re.sub(r"\s+", " ", text).strip()     # 공백 정규화
    # 문장 분리(마침표/개행 기준) 후 중복 제거 (순서 유지)
    parts = re.split(r"(?<=[.!?。])\s+|\n+", text)
    seen, out = set(), []
    for s in parts:
        s = s.strip(" \t-·•")
        key = re.sub(r"\s+", "", s)
        if len(key) < 4 or key in seen:
            continue
        seen.add(key)
        out.append(s)
    # 끝단 자투리(종결부호 없이 잘린 조각) 제거
    if out and not re.search(r"[.!?。]$", out[-1]):
        # 마지막 조각이 앞에서 나온 문장의 접두어이면 버림
        frag = re.sub(r"\s+", "", out[-1])
        if any(re.sub(r"\s+", "", s).startswith(frag) for s in out[:-1]):
            out = out[:-1]
    return " ".join(out)


def summarize(clean):
    if len(clean) < 40:
        return clean  # 짧으면 요약 불필요
    try:
        return chat([{"role": "user", "content": PROMPT_V1.format(desc=clean[:2000])}],
                    max_tokens=512, temperature=0).strip()
    except LLMExhausted as e:
        print(f"  LLM 소진, summary=null: {e}")
        return None
    except Exception as e:
        print(f"  LLM 에러, summary=null: {str(e)[:120]}")
        return None


def load_progress():
    if PROG_PATH.exists():
        try:
            return json.loads(PROG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--rules-only", action="store_true")
    a = ap.parse_args()

    catalog = json.loads(IN_PATH.read_text(encoding="utf-8"))
    targets = catalog if a.limit is None else catalog[:a.limit]
    prog = load_progress()

    # 기존 출력물 로드 (재개)
    done = {}
    if OUT_PATH.exists() and not a.force:
        try:
            for r in json.loads(OUT_PATH.read_text(encoding="utf-8")):
                if r.get("desc_summary") or r.get("_llm_failed"):
                    done[r["report_id"]] = r
        except Exception:
            pass

    out, n_llm, n_skip = [], 0, 0
    for r in targets:
        rid = r["report_id"]
        clean = rule_clean(r.get("설명"), r.get("desc"))
        rec = dict(r)
        rec["desc_clean"] = clean
        if not a.rules_only and rid in done and not a.force:
            rec["desc_summary"] = done[rid].get("desc_summary")
            rec["_llm_failed"] = done[rid].get("_llm_failed", False)
            n_skip += 1
        elif not a.rules_only:
            s = summarize(clean)
            rec["desc_summary"] = s
            rec["_llm_failed"] = s is None
            n_llm += 1
            prog[rid] = {"ok": s is not None, "prompt": "v1"}
            PROG_PATH.write_text(json.dumps(prog, ensure_ascii=False, indent=2), encoding="utf-8")
        out.append(rec)
        tag = "SKIP" if (not a.rules_only and rid in done and not a.force) else ("LLM" if not a.rules_only else "RULE")
        print(f"[{tag}] {rid} clean={len(clean)}자 summary={len(rec.get('desc_summary') or '')}자")

    # limit 실행이어도 전체 카탈로그 기준으로 병합 저장 (부분 덮개 방지)
    if a.limit is not None and OUT_PATH.exists():
        merged = {r["report_id"]: r for r in json.loads(OUT_PATH.read_text(encoding="utf-8"))}
        for r in out:
            merged[r["report_id"]] = r
        full = [merged.get(r["report_id"], r) for r in catalog]
    else:
        full = out if a.limit is None else out  # limit+신규: 부분만
    OUT_PATH.write_text(json.dumps(full, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[W2a] done={len(out)} llm_calls={n_llm} skipped={n_skip} -> {OUT_PATH}")


if __name__ == "__main__":
    main()
