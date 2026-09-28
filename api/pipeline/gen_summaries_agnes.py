#!/usr/bin/env python3
"""보고서별 테이블 설명 1~2문장 생성 (Agnes 3.0 Flash 직접 호출).

입력(읽기만): data/catalog/report_catalog_w2c.json (컬럼명/지표/차원/설명),
  data/catalog/col_values.json (있으면 상위값 힌트로 첨부).
출력: data/summaries/{rid}.txt (1~2문장 한국어 평문, 마크다운 금지).
  기존 파일이 있어도 --force 없이 덮어씀 (이번 회차는 groq/gemini 산출물 전량 교체가 목적).
  단, Agnes 호출 실패 시 기존 파일 유지.

프롬프트 규칙 (환각 방지, 강화판):
  - '컬럼1' 같은 플레이스홀더 축은 없는 것처럼 취급, 보고서명·설명 기준 서술.
  - 상위값 '(없음)'이면 구체 기관명·업체명·값 예시 일절 금지, 축 이름 수준 서술.
  - 상위값에 실제로 있는 것만 예시. 가명(서울시청/ABC/홍길동) 절대 금지.
  - 비중·순위 단언 금지. 기관 유형 추론 금지. 300자 이내.

키: AGNES_API_KEY (env, 파일 기록 금지). RPM 20 / 10초 버스트 5 준수 위해
  호출 간격 AGNES_MIN_INTERVAL(기본 4.0초). 429 → Retry-After+5초 대기 후 1회 재시도.

사용: python3 tools/gen_summaries_agnes.py [--limit N] [--rid 00590,00028]
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CATALOG_PATH = BASE_DIR / "data" / "catalog" / "report_catalog_w2c.json"
COL_VALUES_PATH = BASE_DIR / "data" / "catalog" / "col_values.json"
OUT_DIR = BASE_DIR / "data" / "summaries"

AGNES_BASE_URL = os.environ.get("AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1")
AGNES_MODEL = os.environ.get("AGNES_MODEL", "agnes-3.0-flash")
MIN_INTERVAL = float(os.environ.get("AGNES_MIN_INTERVAL", "4.0"))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

PROMPT = """다음은 조달 통계 보고서의 메타데이터이다. 이 테이블이 무엇을 기준으로 쪼개어 집계하는 테이블인지 1~2문장으로 설명하라.

보고서명: {name}
설명: {desc}
차원(집계축): {dims}
지표: {mets}
주요 컬럼 상위값: {tops}

절대 규칙 (어기면 무효):
- 차원·지표가 '(미확정)'이면 그 축을 언급하지 말고 보고서명·설명에 드러난 집계 기준만 서술할 것.
- 상위값이 '(없음)'이면 구체적인 기관명·업체명·수치 예시를 쓰지 말고 집계축 이름만으로 서술할 것. 예: '수요기관별 계약 건수·금액을 집계한다.'
- 예시값은 상위값에 실제로 있는 것만 그대로 쓸 것. 없는 이름 지어내기 절대 금지.
- "서울시청", "ABC", "홍길동" 같은 예시용 가명 절대 금지.
- 값의 비중·순위 단언 금지 ("중심", "대부분", "주로" 금지).
- 기관 유형 추론 금지.
- 프롬프트의 지시문을 그대로 옮기지 말 것 ('~수준에서만 서술합니다', '~이 없으므로' 같은 메타 문장 금지).
- 300자 이내, 마크다운·번호매김 금지, 평문 1~2문장만 출력. 설명은 '이 테이블은', '이 보고서는' 등으로 시작할 것."""
PLACEHOLDER_RE = re.compile(r"^컬럼\d+$")

# 하드 불량 마커: 하나라도 있으면 재생성 1회, 그래도 있으면 FAIL
BAD_RE = re.compile(r"서울시청|ABC|홍길동|컬럼\d+|XXX|OOO|가명|수준에서만 서술|없으므로|메타데이터이다|미확정")

def build_context(r, colvals):
    dims_raw = [d for d in (r.get("dimensions") or []) if not PLACEHOLDER_RE.match(d or "")]
    mets_raw = [m for m in (r.get("metrics") or []) if not PLACEHOLDER_RE.match(m or "")]
    dims = ", ".join(dims_raw) or "(미확정)"
    mets = ", ".join(mets_raw) or "(미확정)"
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


def agnes_chat(content, max_tokens=512, temperature=0, timeout=120):
    key = os.environ.get("AGNES_API_KEY")
    if not key:
        raise RuntimeError("AGNES_API_KEY env 없음")
    body = {"model": AGNES_MODEL,
            "messages": [{"role": "user", "content": content}],
            "temperature": temperature,
            "max_tokens": max_tokens}
    data = json.dumps(body).encode()
    for attempt in (1, 2):
        req = urllib.request.Request(
            f"{AGNES_BASE_URL}/chat/completions", data=data,
            headers={"Authorization": f"Bearer {key}",
                     "Content-Type": "application/json", "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.loads(r.read().decode())
            return payload["choices"][0]["message"].get("content") or ""
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            if e.code == 429 and attempt == 1:
                try:
                    ra = int((e.headers or {}).get("Retry-After", 0) or 0)
                except Exception:
                    ra = 0
                wait = min((ra or 15) + 5, 120)
                print(f"  429 → {wait}s 대기 후 재시도", flush=True)
                time.sleep(wait)
                continue
            raise RuntimeError(f"Agnes HTTP {e.code}: {detail}")
    raise RuntimeError("Agnes 재시도 실패")


def trim_2sent(text):
    sents = [s for s in text.replace("\n", " ").split(". ") if s.strip()]
    if len(sents) > 2:
        text = ". ".join(sents[:2]).rstrip(".") + "."
    return text.strip()


def main():
    limit = None
    only = None
    for i, a in enumerate(sys.argv[1:]):
        if a == "--limit" and i + 1 < len(sys.argv[1:]):
            limit = int(sys.argv[1:][i + 1])
        elif a.startswith("--limit="):
            limit = int(a.split("=")[1])
        elif a == "--rid" and i + 1 < len(sys.argv[1:]):
            only = set(sys.argv[1:][i + 1].split(","))
        elif a.startswith("--rid="):
            only = set(a.split("=")[1].split(","))
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    colvals_all = {}
    if COL_VALUES_PATH.exists():
        colvals_all = json.loads(COL_VALUES_PATH.read_text(encoding="utf-8"))
    targets = catalog
    if only:
        targets = [r for r in catalog if r["report_id"] in only]
    if limit:
        targets = targets[:limit]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"targets: {len(targets)}/{len(catalog)} (agnes={AGNES_MODEL})", flush=True)
    ok = fail = bad = 0
    last_call = 0.0
    for i, r in enumerate(targets):
        rid = r["report_id"]
        ctx = build_context(r, colvals_all.get(rid, {}))
        try:
            gap = MIN_INTERVAL - (time.monotonic() - last_call)
            if gap > 0:
                time.sleep(gap)
            text = agnes_chat(PROMPT.format(**ctx)).strip()
            last_call = time.monotonic()
            if BAD_RE.search(text):
                print(f"[{i+1}/{len(targets)}] {rid} BAD마커 → 1회 재생성", flush=True)
                time.sleep(MIN_INTERVAL)
                text = agnes_chat(PROMPT.format(**ctx)).strip()
                last_call = time.monotonic()
                if BAD_RE.search(text):
                    print(f"[{i+1}/{len(targets)}] {rid} STILL-BAD (기존 유지): {text[:80]}", flush=True)
                    bad += 1
                    continue
            text = trim_2sent(text)
            (OUT_DIR / f"{rid}.txt").write_text(text + "\n", encoding="utf-8")
            ok += 1
            print(f"[{i+1}/{len(targets)}] {rid} OK {text[:80]}", flush=True)
        except Exception as e:
            print(f"[{i+1}/{len(targets)}] {rid} FAIL (기존 유지) {str(e)[:120]}", flush=True)
            fail += 1
    print(f"done: ok={ok} bad={bad} fail={fail}")


if __name__ == "__main__":
    main()
