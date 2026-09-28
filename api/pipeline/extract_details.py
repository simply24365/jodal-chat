#!/usr/bin/env python3
"""보고서 상세 메타 전수 수집 + 공통/개별 분석 md 생성.

파이프라인 (기존 /tmp/bulk.py + build_det.py 방식을 repo 안으로 이식):
  1. data.g2b.go.kr 접속 (playwright, 실제 브라우저 세션)
  2. 보고서마다 상세(bf) + 프롬프트 xml 수집 — 1초 간격 (rate limit 회피)
  3. data/raw/details_raw.json 저장 → 정제 → data/details131.json
  4. 공통/개별 분석 → docs/details-full-survey.md

사용법:
  uv run python tools/extract_details.py           # 전체 (약 131초+)
  uv run python tools/extract_details.py --limit 5 # 앞 5건만 (동작 확인용)
  uv run python tools/extract_details.py --analyze-only  # 수집 생략, 분석만

기존 public/reports131.json, public/details131.json은 건드리지 않음.
"""
import argparse
import html
import json
import re
import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
RAW_PATH = BASE / "data" / "raw" / "details_raw.json"
CLEAN_PATH = BASE / "data" / "details131.json"
MD_PATH = BASE / "docs" / "details-full-survey.md"
REPORTS_PATH = BASE / "public" / "reports131.json"

RATE_LIMIT_SEC = 0.5  # 보고서 1건당 0.5초 (차단 회피)
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "Chrome/126.0 Safari/537.36")


def collect(limit=None):
    from playwright.sync_api import sync_playwright

    rows = json.loads(REPORTS_PATH.read_text(encoding="utf-8"))
    if limit:
        rows = rows[:limit]
    print(f"수집 대상: {len(rows)}건, {RATE_LIMIT_SEC}초 간격")

    out = {}
    with sync_playwright() as p:
        bw = p.chromium.launch(headless=True, args=["--no-sandbox"])
        pg = bw.new_page(user_agent=UA)
        pg.goto("https://data.g2b.go.kr/", timeout=30000,
                wait_until="networkidle")
        pg.wait_for_timeout(3000)

        # 게스트 ID (getReportInfo의 userId, 세션마다 바뀜)
        try:
            guest = json.loads(pg.evaluate(
                """async()=>{const r=await fetch("""
                """"/ai/AiComm/selectMstrApiUserIdKey.do\""""
                """,{method:"POST",headers:{"Content-Type":"application/json"},"""
                """body:"{}"});return await r.text();}""")).get(
                    "result", "guest03")
        except Exception:
            guest = "guest03"
        print(f"guest userId: {guest}")

        # 목록 API에서 보고서별 objectType + 최신 mstrReptIdVal 확보
        # (getReportInfo의 type은 보고서마다 다름. 예: 00118→3, 00113→55)
        list_rows = []
        for page in (1, 2):
            try:
                txt = pg.evaluate(
                    """async (page)=>{const r=await fetch("""
                    """"/ai/ais/aisc/ReptLst/selectReptSrchV2Lst.do\""""
                    """,{method:"POST",headers:{"Content-Type":"application/json"},"""
                    """body:JSON.stringify({dlSrchParamM:{reptKwSeTy:"",reptNm:"","""
                    """recordCountPerPage:"100",currentPage:page}})});"""
                    """return await r.text();}""", page)
                list_rows.extend(json.loads(txt).get("result", []))
            except Exception as e:
                print(f"목록 {page}p 실패: {e}")
        list_by_id = {r.get("hubReptNo"): r for r in list_rows}
        print(f"목록 확보: {len(list_by_id)}건")

        for i, r in enumerate(rows):
            rid = r["hubReptNo(reptId)"]
            lr = list_by_id.get(rid, {})
            otypes = str(lr.get("objectType", "3")).split(",")
            # 신선한 mstrReptIdVal (목록) + 기존 값 합집합
            mids, seen = [], set()
            for m in ((lr.get("mstrReptIdVal") or "") + "," +
                      (r.get("mstrReptIdVal") or "")).split(","):
                m = m.strip()
                if m and m not in seen:
                    seen.add(m)
                    mids.append(m)
            try:
                det = pg.evaluate(
                    """async ([rid,mid])=>{const x=await fetch("""
                    """"/ai/ais/aisc/ReptDtnr/selectReptBfPritm.do\""""
                    """,{method:"POST",headers:{"Content-Type":"application/json"},"""
                    """body:JSON.stringify({dlSrchParamM:{reptId:rid,hubReptNo:rid,"""
                    """objectType:"",mstrReptIdVal:mid}})});return await x.text();}""",
                    [rid, r.get("mstrReptIdVal")])
                det = json.loads(det).get("result", {})
            except Exception as e:
                det = {"_err": str(e)[:100]}
            prompts = []
            for j, mid in enumerate(mids):
                try:
                    typ = int((otypes[j] if j < len(otypes)
                               else otypes[0]).strip() or 3)
                except ValueError:
                    typ = 3
                try:
                    pi = pg.evaluate(
                        """async ([u,oid,typ])=>{const x=await fetch("""
                        """"/portal/app/getReportInfo.json\""""
                        """,{method:"POST","""
                        """headers:{"Content-Type":"application/json"},"""
                        """body:JSON.stringify({userId:u,"""
                        """objectId:oid,type:typ})});return await x.text();}""",
                        [guest, mid, typ])
                    prompts.append({"objectId": mid, "type": typ,
                                    "xml": pi})
                except Exception as e:
                    prompts.append({"objectId": mid, "type": typ,
                                    "_err": str(e)[:100]})
            out[rid] = {"bf": {k: det.get(k) for k in (
                "reptNm", "mstrReptNoVal", "hubReptDscrSummCn",
                "hubReptDtlDscr", "reptSubNm", "chgDt", "inptDt",
                "fileDtaUrl")}, "prompts": prompts}
            print(f"  {i + 1}/{len(rows)} {rid} "
                  f"prompts={len(prompts)}", flush=True)
            time.sleep(RATE_LIMIT_SEC)  # 차단 회피: 1초에 1개
        bw.close()

    RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
    RAW_PATH.write_text(json.dumps(out, ensure_ascii=False),
                        encoding="utf-8")
    print(f"원시 저장: {RAW_PATH} ({len(out)}건)")
    return out


def clean_entity(s):
    if not s:
        return ""
    s = s.replace("&#40;", "(").replace("&#41;", ")")
    s = s.replace("&nbsp;", " ")
    return html.unescape(s).strip()


def build_clean(raw):
    out = {}
    for rid, v in raw.items():
        bf = v.get("bf") or {}
        prompts, seen = [], set()
        for p in v.get("prompts", []):
            x = p.get("xml") or ""
            if "<promptList>" not in x:
                continue
            blocks = re.findall(
                r"<promptList><id>.*?</promptList>(?=<promptList>|"
                r"</promptList>)", x, re.S)
            for b in blocks:
                def g(t):
                    m = re.search(r"<%s>(.*?)</%s>" % (t, t), b, re.S)
                    return m.group(1).strip() if m else ""
                title, pin = g("title"), g("pin") or "99"
                if (title, pin) in seen:
                    continue
                seen.add((title, pin))
                sug = re.findall(r"<displayName>(.*?)</displayName>", b)
                prompts.append({
                    "pin": int(pin) if pin.isdigit() else 99,
                    "title": title,
                    "ui": g("exUiType") or g("controlType"),
                    "opts": sug[:20], "optsTotal": len(sug)})
        prompts.sort(key=lambda r: r["pin"])
        out[rid] = {
            "desc": clean_entity(
                bf.get("hubReptDtlDscr") or bf.get("hubReptDscrSummCn")),
            "sub": clean_entity(bf.get("reptSubNm")),
            "prompts": prompts}
    CLEAN_PATH.write_text(json.dumps(out, ensure_ascii=False),
                          encoding="utf-8")
    print(f"정제 저장: {CLEAN_PATH} ({len(out)}건)")
    return out


def norm_title(t):
    t = re.sub(r"\(From\)|\(To\)", "", t).replace("_전일", "")
    return re.sub(r"\(.*", "", t).strip()


def analyze(data):
    lines = []
    today = date.today().isoformat()
    n = len(data)
    counts = sorted([len(v["prompts"]) for v in data.values()])
    avg = sum(counts) / n if n else 0
    zero = [k for k, v in data.items() if not v["prompts"]]
    ui_all = Counter(p["ui"] for v in data.values()
                     for p in v["prompts"])
    norm = Counter((norm_title(p["title"]), p["ui"])
                   for v in data.values() for p in v["prompts"])
    opts_total = sum(p["optsTotal"] for v in data.values()
                     for p in v["prompts"])

    lines.append(f"# 보고서 상세 메타 전수 분석 ({n}건)\n")
    lines.append(f"> 생성일: {today}, 원본: `{CLEAN_PATH.name}`\n")
    lines.append("## 1. 전체 요약\n")
    lines.append(f"- 조건 있음: **{n - len(zero)}건**, 없음: **{len(zero)}건**")
    lines.append(f"- 조건 수: 최소 {counts[0]}, 최대 {counts[-1]}, "
                 f"평균 {avg:.1f}")
    lines.append(f"- 고정 옵션값 총합: {opts_total:,}개\n")
    lines.append("### UI 타입 분포\n")
    for ui, c in ui_all.most_common():
        lines.append(f"- `{ui}`: {c}")
    lines.append("\n### 공통 조건 Top 20 (정규화)\n")
    lines.append("| 횟수 | 조건명 | UI |")
    lines.append("|---|---|---|")
    for (t, u), c in norm.most_common(20):
        lines.append(f"| {c} | {t} | `{u}` |")

    lines.append("\n## 2. 개별 specific (보고서별 특이 조건)\n")
    lines.append("> 전체 Top 20에 들지 못한, 해당 보고서 고유 조건만 나열. "
                 "대화 설계 시 슬롯 예외 케이스.\n")
    top20 = {k for k, _ in norm.most_common(20)}
    for rid in sorted(data):
        uniq = [(p["title"], p["ui"], p["optsTotal"])
                for p in data[rid]["prompts"]
                if (norm_title(p["title"]), p["ui"]) not in top20]
        if uniq:
            lines.append(f"### {rid}")
            for t, u, o in uniq:
                lines.append(f"- [{u}] {t} (옵션 {o}개)")

    lines.append("\n## 3. 조건 0건 보고서 목록\n")
    lines.append("> `진짜 조건 없음` vs `수집 누락` 판별 필요.\n")
    for rid in sorted(zero):
        lines.append(f"- {rid}")

    MD_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"분석 저장: {MD_PATH}")
    print(f"  조건있음 {n - len(zero)} / 없음 {len(zero)} / "
          f"평균조건 {avg:.1f}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--analyze-only", action="store_true")
    a = ap.parse_args()
    if a.analyze_only:
        raw = json.loads(RAW_PATH.read_text(encoding="utf-8"))
    else:
        raw = collect(limit=a.limit)
    data = build_clean(raw)
    analyze(data)


if __name__ == "__main__":
    sys.exit(main())
