#!/usr/bin/env python3
"""조달데이터허브 보고서 출력 컬럼/지표 자동 수집기.

단일 보고서(또는 지정 보고서)를 웹스퀘어 UI에서 실제로 조회하고
MSTR 내보내기 옵션을 통해 엑셀을 다운로드한 후,
헤더와 샘플 행을 파싱하여 출력 스키마(차원, 지표, 단위, 샘플)를 JSON으로 구조화합니다.

사용법:
  uv run --with playwright --with openpyxl python tools/extract_report_columns.py --report-id 00118
"""

import argparse
import glob
import json
import os
import re
import sys
import time
from pathlib import Path
import openpyxl

BASE_DIR = Path(__file__).resolve().parent.parent
REPORTS_PATH = BASE_DIR / "public" / "reports131.json"
DATA_COLUMNS_DIR = BASE_DIR / "data" / "columns"
RAW_EXCEL_DIR = BASE_DIR / "data" / "raw_excel"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def classify_column(col_name, sample_vals, is_numeric):
    name = col_name.strip()
    # 지표 판별 키워드
    metric_keywords = ["금액", "수량", "단가", "건수", "율", "비율", "점수", "평균", "합계", "증감"]
    date_keywords = ["일자", "일시", "년월", "년도", "기간", "기한", "일"]
    code_keywords = ["코드", "번호", "차수", "사업자등록번호", "식별"]
    flag_keywords = ["여부", "구분", "유형", "상태"]

    if any(k in name for k in metric_keywords) or (is_numeric and not any(k in name for k in code_keywords + date_keywords)):
        return "metric"
    if any(k in name for k in code_keywords):
        return "identifier"
    if any(k in name for k in date_keywords):
        return "date"
    if any(k in name for k in flag_keywords):
        return "flag"
    return "dimension"


def extract_report(report_id, headless=True, timeout_sec=40):
    from playwright.sync_api import sync_playwright

    reports = json.loads(REPORTS_PATH.read_text(encoding="utf-8"))
    report = next((r for r in reports if str(r.get("hubReptNo(reptId)")).strip() == report_id), None)
    if not report:
        raise ValueError(f"보고서 ID '{report_id}'를 찾을 수 없습니다.")

    report_name = report.get("보고서명")
    official_id = report.get("보고서ID")
    print(f"[*] 대상 보고서: [{report_id}] {report_name} (보고서ID: {official_id})")

    DATA_COLUMNS_DIR.mkdir(parents=True, exist_ok=True)
    RAW_EXCEL_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        bw = p.chromium.launch(headless=headless, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = bw.new_context(
            user_agent=UA,
            viewport={"width": 1440, "height": 900},
            accept_downloads=True
        )

        pg = context.new_page()
        # 공식 목록 페이지 접근 (reptNm 파라미터로 해당 보고서만 1건 조회)
        url = f"https://data.g2b.go.kr/link/AISC001_01/?reptNm={official_id}"
        print(f"[*] [1/5] 목록 페이지 접속: {url}")
        pg.goto(url, wait_until="networkidle", timeout=35000)
        pg.wait_for_timeout(3000)

        # 보고서 제목 링크 클릭하여 팝업 오픈
        print("[*] [2/5] 보고서 상세 팝업 오픈...")
        title_selector = "#mf_wfm_container_reptLst_0_reptNm"
        try:
            pg.wait_for_selector(title_selector, timeout=10000)
        except Exception:
            # 파라미터 검색으로 안 뜰 경우 첫 번째 행 클릭
            title_selector = "a[id*='reptLst_0_reptNm']"

        with context.expect_page(timeout=20000) as popup_info:
            pg.click(title_selector)
        popup = popup_info.value
        popup.wait_for_load_state("networkidle", timeout=15000)
        popup.wait_for_timeout(3000)

        # 검색 실행
        print("[*] [3/5] 검색(조회) 실행...")
        search_btn = "#mf_popupCnts_btnS0001"
        popup.wait_for_selector(search_btn, timeout=10000)
        popup.click(search_btn)

        # 검색 결과 렌더링 대기 (엑셀다운로드 버튼 활성화)
        excel_btn = "#mf_popupCnts_btnExcelDown:not(.hide)"
        print("[*] [3/5] 조회 결과 대기 중 (최대 30초)...")
        popup.wait_for_selector(excel_btn, timeout=35000)
        popup.wait_for_timeout(2000)

        # 엑셀 다운로드 버튼 클릭 -> MSTR 내보내기 옵션 팝업 열기
        print("[*] [4/5] MSTR 내보내기 옵션 창 열기...")
        with context.expect_page(timeout=20000) as opt_info:
            popup.evaluate("""() => {
                const cntsComp = $p.getComponentById('mf_popupCnts');
                const win = cntsComp.getWindow();
                win.scwin.btnExcelDown_onclick();
            }""")
        export_page = opt_info.value
        export_page.wait_for_load_state("networkidle", timeout=15000)
        export_page.wait_for_timeout(2000)

        # 내보내기 버튼(id=3131) 클릭 및 다운로드 대기
        print("[*] [5/5] '내보내기' 클릭 및 엑셀 다운로드...")
        with export_page.expect_download(timeout=35000) as dl_info:
            export_page.click('input[id="3131"]')

        download = dl_info.value
        filename = download.suggested_filename
        excel_path = RAW_EXCEL_DIR / f"{report_id}_{filename}"
        download.save_as(str(excel_path))
        print(f"[+] 엑셀 파일 저장 완료: {excel_path}")

        bw.close()

    # 엑셀 파일 분석
    print("[*] 엑셀 메타데이터 파싱 중...")
    meta = parse_excel_metadata(excel_path, report_id, report_name, official_id)
    out_json = DATA_COLUMNS_DIR / f"{report_id}_columns.json"
    out_json.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[+] 구조화 메타데이터 저장 완료: {out_json}")
    return meta


def parse_excel_metadata(excel_path, report_id, report_name, official_id):
    wb = openpyxl.load_workbook(str(excel_path), data_only=True)
    ws = wb.active

    # 1. 헤더 행 찾기 (보통 Row 5, 컬럼 헤더가 5개 이상 연속 존재하는 행)
    header_row = None
    for r in range(1, min(15, ws.max_row + 1)):
        row_vals = [ws.cell(row=r, column=c).value for c in range(1, min(50, ws.max_column + 1))]
        non_empty = [v for v in row_vals if v is not None and str(v).strip()]
        if len(non_empty) >= 5 and not str(non_empty[0]).startswith("검색조건"):
            header_row = r
            break

    if not header_row:
        header_row = 5

    # 2. 상단 메타데이터 파싱 (검색조건, 보고서명)
    prompt_info = ws.cell(row=1, column=1).value or ""
    full_report_title = ws.cell(row=3, column=1).value or ""

    # 3. 컬럼 파싱
    columns = []
    for c in range(1, ws.max_column + 1):
        col_name = ws.cell(row=header_row, column=c).value
        if col_name is None or not str(col_name).strip():
            continue
        col_name = str(col_name).strip()

        # 샘플 값 수집
        sample_vals = []
        for r in range(header_row + 1, min(header_row + 21, ws.max_row + 1)):
            v = ws.cell(row=r, column=c).value
            if v is not None:
                sample_vals.append(v)

        is_numeric = all(isinstance(v, (int, float)) for v in sample_vals) if sample_vals else False
        category = classify_column(col_name, sample_vals, is_numeric)

        # 표시용 샘플
        display_samples = [str(x)[:40] for x in sample_vals[:3]]

        columns.append({
            "index": c,
            "name": col_name,
            "category": category,
            "is_metric": category == "metric",
            "is_numeric": is_numeric,
            "sample_values": display_samples
        })

    metrics = [c["name"] for c in columns if c["is_metric"]]
    dimensions = [c["name"] for c in columns if c["category"] == "dimension"]
    identifiers = [c["name"] for c in columns if c["category"] == "identifier"]
    dates = [c["name"] for c in columns if c["category"] == "date"]

    return {
        "report_id": report_id,
        "report_name": report_name,
        "official_id": official_id,
        "full_report_title": full_report_title,
        "prompt_summary": prompt_info[:300] if prompt_info else "",
        "sheet_name": ws.title,
        "total_data_rows": max(0, ws.max_row - header_row),
        "total_columns": len(columns),
        "header_row_index": header_row,
        "metrics_summary": {
            "count": len(metrics),
            "metrics": metrics
        },
        "dimensions_summary": {
            "count": len(dimensions),
            "dimensions": dimensions
        },
        "identifiers_summary": {
            "count": len(identifiers),
            "identifiers": identifiers
        },
        "dates_summary": {
            "count": len(dates),
            "dates": dates
        },
        "columns": columns
    }


def main():
    parser = argparse.ArgumentParser(description="보고서 출력 컬럼/지표 엑셀 자동 수집기")
    parser.add_argument("--report-id", default="00118", help="보고서 hubReptNo (기본값: 00118)")
    parser.add_argument("--headful", action="store_true", help="브라우저 화면 표시 여부")
    args = parser.parse_args()

    meta = extract_report(report_id=args.report_id, headless=not args.headful)

    print("\n" + "=" * 60)
    print(f"📊 [{meta['report_id']}] {meta['report_name']} 출력 메타데이터")
    print("=" * 60)
    print(f"총 컬럼 수: {meta['total_columns']}개 (데이터 행: {meta['total_data_rows']}건)")
    print(f"\n[핵심 지표 (Metrics, {meta['metrics_summary']['count']}개)]")
    for m in meta['metrics_summary']['metrics']:
        print(f"  - 📈 {m}")
    print(f"\n[차원/속성 (Dimensions, {meta['dimensions_summary']['count']}개)]")
    for d in meta['dimensions_summary']['dimensions'][:10]:
        print(f"  - 🏷️ {d}")
    if len(meta['dimensions_summary']['dimensions']) > 10:
        print(f"  ... 외 {len(meta['dimensions_summary']['dimensions']) - 10}개")
    print(f"\n[일자/기한 (Dates, {meta['dates_summary']['count']}개)]")
    for dt in meta['dates_summary']['dates']:
        print(f"  - 📅 {dt}")
    print("=" * 60)


if __name__ == "__main__":
    main()
