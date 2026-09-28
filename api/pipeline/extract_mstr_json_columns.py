#!/usr/bin/env python3
"""MSTR taskProc JSON 경유 컬럼 수집기 (엑셀버튼 미노출 보고서용, 제3경로).

배경: 일부 반정형 보고서(예: 00030 조달요청 내역)는 조회 후에도
  #mf_popupCnts_btnExcelDown 의 .hide 가 제거되지 않아 엑셀 다운로드가 불가하다.
  그러나 mstrFrame 은 taskProc(JSON) 응답으로 실제 데이터를 수신한다.
  이 JSON 의 attribute 정의({"t":12,"n":"컬럼명"})를 파싱하면 출력 컬럼을 확보할 수 있다.

사용법:
  uv run --with playwright python tools/extract_mstr_json_columns.py --report-id 00030
  uv run --with playwright python tools/extract_mstr_json_columns.py --all-missing
"""
import argparse
import json
import re
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
REPORTS_PATH = BASE_DIR / "public" / "reports131.json"
DATA_COLUMNS_DIR = BASE_DIR / "data" / "columns"
RAW_JSON_DIR = BASE_DIR / "data" / "raw_mstr_json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def parse_columns_from_task_json(text):
    """taskProc 응답 텍스트에서 attribute(t:12) 컬럼명 추출 (등장순, 중복제거)."""
    attrs = re.findall(r'"t":12,"st":\d+,"n":"([^"]+)"', text)
    seen, ordered = set(), []
    for a in attrs:
        if a not in seen:
            seen.add(a)
            ordered.append(a)
    # metric 후보 (t:7)
    mets = re.findall(r'"t":7,[^}]*?"n":"([^"]+)"', text)
    mets = list(dict.fromkeys(mets))
    return ordered, mets


def classify_column(name):
    n = name.strip()
    if any(k in n for k in ["금액", "수량", "단가", "건수", "율", "비율", "점수", "평균", "합계", "증감", "기간(일)"]):
        return "metric"
    if any(k in n for k in ["코드", "번호", "차수", "사업자등록번호", "식별"]):
        return "identifier"
    if any(k in n for k in ["일자", "일시", "년월", "년도", "기간", "기한", "납품기한"]):
        return "date"
    if any(k in n for k in ["여부", "구분", "유형", "상태"]):
        return "flag"
    return "dimension"


def extract_via_json(report_id, headless=True, wait_sec=30):
    from playwright.sync_api import sync_playwright
    reports = json.loads(REPORTS_PATH.read_text(encoding="utf-8"))
    r = next(x for x in reports if str(x.get("hubReptNo(reptId)")).strip() == report_id)
    official_id, name = r.get("보고서ID"), r.get("보고서명")
    print(f"[*] [{report_id}] {name} ({official_id})", flush=True)
    DATA_COLUMNS_DIR.mkdir(parents=True, exist_ok=True)
    RAW_JSON_DIR.mkdir(parents=True, exist_ok=True)
    bodies, alerts = [], []
    with sync_playwright() as p:
        bw = p.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-setuid-sandbox"])
        ctx = bw.new_context(user_agent=UA, viewport={"width": 1440, "height": 900})
        pg = ctx.new_page()
        pg.goto(f"https://data.g2b.go.kr/link/AISC001_01/?reptNm={official_id}",
                wait_until="networkidle", timeout=60_000)
        pg.wait_for_timeout(2000)
        sel = "#mf_wfm_container_reptLst_0_reptNm"
        try:
            pg.wait_for_selector(sel, timeout=10_000)
        except Exception:
            sel = "a[id*='reptLst_0_reptNm']"
        with ctx.expect_page(timeout=60_000) as pi:
            pg.click(sel)
        popup = pi.value
        popup.on("dialog", lambda d: (alerts.append(d.message), d.accept()))

        def on_resp(resp):
            try:
                if "taskProc" in resp.url:
                    bodies.append(resp.body())
            except Exception:
                pass
        popup.on("response", on_resp)
        try:
            popup.wait_for_load_state("networkidle", timeout=60_000)
        except Exception:
            pass
        popup.wait_for_timeout(2000)
        popup.wait_for_selector("#mf_popupCnts_btnS0001", timeout=15_000)
        popup.click("#mf_popupCnts_btnS0001")
        print(f"[*] 검색 클릭 → taskProc {wait_sec}s 수집", flush=True)
        popup.wait_for_timeout(wait_sec * 1000)
        bw.close()
    print(f"[*] taskProc 응답 {len(bodies)}건, alerts={alerts}", flush=True)
    # 가장 큰 JSON = 실데이터
    cands = []
    for b in bodies:
        try:
            t = b.decode("utf-8", errors="ignore")
            if '"t":12' in t:
                cands.append(t)
        except Exception:
            pass
    if not cands:
        return {"status": "NO_DATA_JSON", "responses": len(bodies)}
    raw = max(cands, key=len)
    (RAW_JSON_DIR / f"{report_id}_taskProc.json").write_text(raw, encoding="utf-8")
    cols, mets = parse_columns_from_task_json(raw)
    columns = [{"index": i + 1, "name": c, "category": classify_column(c),
                "is_metric": classify_column(c) == "metric",
                "is_numeric": False, "sample_values": []} for i, c in enumerate(cols)]
    meta = {"report_id": report_id, "report_name": name, "official_id": official_id,
            "export_path": "mstr-taskProc-json (엑셀버튼 미노출 우회)",
            "total_columns": len(columns), "total_data_rows": -1,
            "metrics_summary": {"count": len(mets), "metrics": mets},
            "dimensions_summary": {"count": sum(1 for c in columns if c["category"] == "dimension"),
                                   "dimensions": [c["name"] for c in columns if c["category"] == "dimension"]},
            "columns": columns}
    out = DATA_COLUMNS_DIR / f"{report_id}_columns.json"
    out.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] {out} 컬럼 {len(columns)}개", flush=True)
    return {"status": "SUCCESS", "total_columns": len(columns)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-id", default="00030")
    ap.add_argument("--all-missing", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--wait-sec", type=int, default=30)
    a = ap.parse_args()
    if a.all_missing:
        reports = json.loads(REPORTS_PATH.read_text(encoding="utf-8"))
        targets = [str(r.get("hubReptNo(reptId)")).strip() for r in reports
                   if r.get("mstrFrmt") == "엠060003"]
        summary = {}
        for rid in targets:
            out = DATA_COLUMNS_DIR / f"{rid}_columns.json"
            if out.exists() and not a.force:
                print(f"[SKIP] {rid}")
                summary[rid] = "SKIPPED"
                continue
            summary[rid] = extract_via_json(rid, wait_sec=a.wait_sec).get("status")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return
    print(json.dumps(extract_via_json(a.report_id, wait_sec=a.wait_sec), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
