#!/usr/bin/env python3
"""보고서별 테이블 설명 1~2문장 생성 (LLM, groq->gemini 폴백).

입력(읽기만): data/catalog/report_catalog_w2c.json (컬럼명/지표/차원/설명),
  data/catalog/col_values.json (있으면 상위값 힌트로 첨부).
출력: data/summaries/{rid}.txt (1~2문장 한국어 평문, 마크다운 금지).
  기존 파일 있으면 스킵 (재실행 안전). --force로 재생성.

프롬프트 규칙 (환각 방지):
  - 컬럼명에 있는 축(수요기관, 품명, 업체명…)만 언급. 값 단언 금지.
  - 최빈값은 경향 예시로만 ("~등이 상위에观측됨" 금지 → "상위에는 A, B 등이 있음").
  - 기관 유형 추론 금지 ("군부대 중심" 같은 단정 금지).
  - 2문장 초과 금지, 300자 이내.

사용: python3 tools/gen_summaries.py [--limit N] [--force]
  키: GROQ_API_KEY / GEMINI_API_KEY (env, 파일 기록 금지).
"""
import json
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CATALOG_PATH = BASE_DIR / "data" / "catalog" / "report_catalog_w2c.json"
COL_VALUES_PATH = BASE_DIR / "data" / "catalog" / "col_values.json"
OUT_DIR = BASE_DIR / "data" / "summaries"

sys.path.insert(0, str(BASE_DIR))
from llm_chain import LLMExhausted, chat

PROMPT = """다음은 조달 통계 보고서의 메타데이터이다. 이 테이블이 무엇을 기준으로 쪼개어 집계하는 테이블인지 1~2문장으로 설명하라.

보고서명: {name}
설명: {desc}
차원(집계축): {dims}
지표: {mets}
주요 컬럼 상위값: {tops}

절대 규칙 (어기면 무효):
- 컬럼명에 있는 축만 언급하라.
- 상위값에 실제로 있는 기관명·값만 예시로 쓸 것. 없는 이름 지어내기 절대 금지.
- "서울시청", "ABC", "홍길동" 같은 예시용 가명 절대 금지.
- 값의 비중·순위 단언 금지 ("중심", "대부분", "주로" 금지).
- 기관 유형 추론 금지.
- 300자 이내, 마크다운·번호매김 금지, 평문 1~2문장만 출력."""


def build_context(r, colvals):
    dims = ", ".join(r.get("dimensions") or []) or "(없음)"
    mets = ", ".join(r.get("metrics") or []) or "(없음)"
    desc = (r.get("desc_summary") or r.get("desc_clean") or r.get("설명") or "")[:400]
    tops = []
    for h, c in (colvals.get("columns") or {}).items():
        top = [k for _, k in (c.get("top") or [])[:5]]
        if not top:
            continue
        tops.append(f"{h}={','.join(top)}")
        if len(tops) >= 4:
            break
    return {
        "name": r.get("보고서명") or "",
        "desc": desc.strip(),
        "dims": dims,
        "mets": mets,
        "tops": "; ".join(tops) or "(없음)",
    }


def main():
    limit = None
    force = False
    for a in sys.argv[1:]:
        if a == "--force":
            force = True
        elif a.startswith("--limit"):
            limit = int(a.split("=")[1] if "=" in a else sys.argv[sys.argv.index(a) + 1])
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    colvals = {}
    if COL_VALUES_PATH.exists():
        colvals = json.loads(COL_VALUES_PATH.read_text(encoding="utf-8"))
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    targets = [r for r in catalog if force or not (OUT_DIR / f"{r['report_id']}.txt").exists()]
    if limit:
        targets = targets[:limit]
    print(f"targets: {len(targets)}/{len(catalog)} (force={force})")
    ok = fail = 0
    for i, r in enumerate(targets):
        rid = r["report_id"]
        ctx = build_context(r, colvals.get(rid, {}))
        try:
            # gpt-oss reasoning 선점 대비 여유 토큰. 출력은 2문장으로 잘라냄.
            text = chat(
                [{"role": "user", "content": PROMPT.format(**ctx)}],
                max_tokens=1024,
                temperature=0,
            ).strip()
        except LLMExhausted as e:
            print(f"[{i+1}/{len(targets)}] {rid} FAIL {str(e)[:100]}")
            fail += 1
            continue
        # 3문장 이상이면 자름 (안전망)
        sents = [s for s in text.replace("\n", " ").split(". ") if s.strip()]
        if len(sents) > 2:
            text = ". ".join(sents[:2]).rstrip(".") + "."
        (OUT_DIR / f"{rid}.txt").write_text(text + "\n", encoding="utf-8")
        ok += 1
        print(f"[{i+1}/{len(targets)}] {rid} OK {text[:80]}")
        time.sleep(1)
    print(f"done: ok={ok} fail={fail}")


if __name__ == "__main__":
    main()
