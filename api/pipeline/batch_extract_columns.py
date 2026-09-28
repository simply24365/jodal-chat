#!/usr/bin/env python3
"""조달데이터허브 131개 보고서 출력 메타데이터 일괄 배치 수집기 (비동기 병렬 버전).

전략서(docs/131-full-extraction-strategy.md) 기반의 무인 실행 파이프라인:
  - asyncio + Playwright async API로 동시 N건(기본 5) 병렬 처리
  - 기존 산출물({rid}_columns.json) 또는 SUCCESS 기록 보유 시 자동 스킵 (Resume 지원)
  - 조회 완료 대기 타임아웃 180초 (MSTR 렌더링 지연 흡수)
  - Alert 다이얼로그 가로채기 및 사유 로깅
  - 보고서별 브라우저 컨텍스트 격리로 메모리 누수 방지
  - 결과 진행상황(batch_progress.json) 실시간 저장

사용법:
  uv run --with playwright --with openpyxl python tools/batch_extract_columns.py --limit 2
  uv run --with playwright --with openpyxl python tools/batch_extract_columns.py --dry-run
  uv run --with playwright --with openpyxl python tools/batch_extract_columns.py --workers 5
  uv run --with playwright --with openpyxl python tools/batch_extract_columns.py --force

Tier 2: 검색 전 팝업 내 빈 날짜/년월/셀렉트박스에 룰 기반 기본값(2025년) 자동 주입.
내보내기 분기: A(scwin 핸들러 직접호출) → 실패 시 B(엑셀버튼 실클릭→새탭 PreferenceForm).
조회완료 미감지 시 폴백: RW 대시보드 DOM 스크랩 → taskProc JSON 직독(C경로).
"""

import argparse
import asyncio
import json
import random
import re
import time
from datetime import datetime
from pathlib import Path

import openpyxl

BASE_DIR = Path(__file__).resolve().parent.parent
REPORTS_PATH = BASE_DIR / "public" / "reports131.json"
DATA_COLUMNS_DIR = BASE_DIR / "data" / "columns"
RAW_EXCEL_DIR = BASE_DIR / "data" / "raw_excel"
RAW_JSON_DIR = BASE_DIR / "data" / "raw_mstr_json"
PROGRESS_FILE = DATA_COLUMNS_DIR / "batch_progress.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

# ── 타임아웃 상수 (ms) ────────────────────────────────────────────
NAV_TIMEOUT_MS = 60_000          # 페이지 이동 / networkidle
SEARCH_TIMEOUT_MS = 180_000      # 조회 완료 대기 (기존 25초 -> 180초)
EXPORT_WINDOW_TIMEOUT_MS = 60_000  # 내보내기 옵션 창 오픈 대기
DOWNLOAD_TIMEOUT_MS = 180_000    # 엑셀 다운로드 대기

# ── Tier 2: 빈 조회조건 룰 기반 자동 주입 (전략서 §3.1) ──────────
# 검색 실행 전 팝업 내 "비어 있는" 날짜/년월/셀렉트 컴포넌트만 골라 기본값 주입.
# 이미 값이 있으면 건드리지 않음 -> 기본조건 자동입력 보고서(00118 등)에는 무해.
# 일자/년월: 최근 1년 (From=12개월 전 1일, To=오늘). 넓은 범위(2025년 통년 등)는
SEED_CODES = {
    "수요기관최상위기관": ("B410002", "한국전력공사"),
    "수요기관소재시군구": ("SKIP", ""),
    "발주기관소재시군구": ("SKIP", ""),
    "업체소재시군구": ("SKIP", ""),
    "수요기관": ("B500001", "한국수자원공사"),
    "발주기관": ("B500001", "한국수자원공사"),
    "물품분류": ("42192209", "휠체어경사로"),
    "업체": ("SKIP", ""),
    "조달업무구분목록": ("물품", "물품"),
}
# SKIP: 지역 하위선택 (상위 시도가 없으면 단독 조회 불가). 주입하지 않음.
# 업체 SKIP: 물품+업체+기관 AND 시 0건 가능. 값 확보 목적은 물품+기관이면 충분.
FILL_DEFAULTS_JS = """() => {
    const filled = [];
    const trySet = (comp, value) => {
        try { comp.setValue(value); } catch (e) { return false; }
        try { return String(comp.getValue()).trim() !== ''; } catch (e) { return true; }
    };
    const fmtOf = (comp) => {
        try { return String(comp.getFormat() || ''); } catch (e) { return ''; }
    };

    // 1) 날짜/년월 계열 주입. 값이 있어도 1년 윈도우로 덮어씀.
    // 이유: 팝업 기본값(오늘 하루)은 특정품목(00605)처럼 0건을 유발.
    // 전략서 3.2 (0건 시 과거 확장)의 역방향: 처음부터 1년으로 조회.
    document.querySelectorAll('input[type="text"][id]').forEach(el => {
        const comp = $p.getComponentById(el.id);
        if (!comp || typeof comp.setValue !== 'function' || typeof comp.getValue !== 'function') return;
        // NOTE: 빈 검사 없음. 날짜는 기본값 있어도 1년 윈도우로 덮어씀.
        const id = el.id.toLowerCase();
        const cls = String(el.className || '').toLowerCase();
        const fmt = fmtOf(comp).toLowerCase();
        const dateish = fmt.includes('yyyy') || /date|calendar|yymm|udcdate/.test(cls) || /date|yymm|dt[0-9_]|ibx.*day/.test(id);
        if (!dateish) return;

        const isYymm = (fmt.includes('mm') && !fmt.includes('dd')) || /yymm/.test(cls) || /yymm/.test(id);
        const isFrom = /from|frdt|start|bgdt|begin|str(?!end)/.test(id);
        const sep = fmt.includes('-') ? '-' : '';
        let val;
        // 최근 1년 윈도우: To=오늘, From=12개월 전 1일 (형식은 기존 fmt/sep 규칙).
        const now = new Date();
        const pad = (n, l) => String(n).padStart(l, '0');
        const toY = now.getFullYear(), toM = now.getMonth() + 1;
        let fromY = toY - 1, fromM = toM;
        if (isYymm) {
            val = isFrom ? `${fromY}${sep}${pad(fromM, 2)}` : `${toY}${sep}${pad(toM, 2)}`;
        } else {
            val = isFrom ? `${fromY}${sep}${pad(fromM, 2)}${sep}01` : `${toY}${sep}${pad(toM, 2)}${sep}${pad(new Date(toY, toM, 0).getDate(), 2)}`;
        }
        if (trySet(comp, val)) filled.push([el.id, val]);
    });

    // 2) 빈 단일 셀렉트박스는 두 번째 옵션(실유효값) 선택
    document.querySelectorAll('select[id]').forEach(el => {
        const comp = $p.getComponentById(el.id);
        if (!comp || typeof comp.setSelectedIndex !== 'function') return;
        let idx = -1;
        try { idx = comp.getSelectedIndex(); } catch (e) { return; }
        if (idx >= 0) return;
        try {
            const n = el.options.length;
            const target = n > 1 ? 1 : 0;
            comp.setSelectedIndex(target);
            filled.push([el.id, 'idx:' + target]);
        } catch (e) {}
    });

    return filled;
}"""

# Tier 3 JS: SEED_CODES를 받아 빈 코드팝업 인풋에 코드 setValue.
# 매칭: input 주변 label/타이틀 텍스트에 key substring 포함 + 값 비어있음.
# 세부품명/물품식별은 물품분류 종속 하위선택이라 건너뜀 (상위 코드만으로 조회됨).
FILL_SEED_JS = """(seeds) => {
    const filled = [];
    // 코드입력칸: id가 _3으로 끝나는 hidden input.
    // 라벨 = 같은 TR에서 input 든 TD 바로 앞의 비어있지 않은 TD 텍스트
    // (th는 '조회물품/업체/조회기관' 그룹 라벨이라 구분 불가).
    const inputs = [...document.querySelectorAll('input[id$="_3"]')];
    for (const el of inputs) {
        const td = el.closest('td');
        let label = '';
        if (td) {
            let sib = td.previousElementSibling;
            while (sib) {
                const t = (sib.innerText || '').replace(/\s+/g, ' ').trim();
                if (t) { label = t; break; }
                sib = sib.previousElementSibling;
            }
        }
        if (!label) continue;
        let comp;
        try { comp = $p.getComponentById(el.id); } catch (e) { continue; }
        if (!comp || typeof comp.setValue !== 'function' || typeof comp.getValue !== 'function') continue;
        let v = '';
        try { v = String(comp.getValue() || '').trim(); } catch (e) { continue; }
        if (v) continue;
        // 긴 키 우선: '수요기관소재시군구' > '수요기관' 처럼 포함관계 키의 오매칭 방지.
        const keys = Object.keys(seeds).sort((a, b) => b.length - a.length);
        for (const key of keys) {
            const [code, name] = seeds[key];
            if (label.includes(key)) {
                if (code === "SKIP") break; // 지역 하위선택: 단독 주입 불가, 스킵
                try {
                    comp.setValue(code);
                    let ok = false;
                    try { ok = String(comp.getValue() || '').trim() !== ''; } catch (e) { ok = true; }
                    if (ok) filled.push([el.id, key + '=' + code]);
                } catch (e) {}
                break;
            }
        }
    }
    return filled;
}"""


# ── RW(대시보드)형 폴백: 엑셀버튼이 안 뜨는 Visual Insight 대시보드 처리 ──
# mstrFrame 안의 보이는 <table> 중 최대 크기(행×열)를 데이터 그리드로 간주.
RW_TABLE_PROBE_JS = """() => {
    let best = null;
    for (const t of document.querySelectorAll('table')) {
        try {
            const rect = t.getBoundingClientRect();
            if (rect.width < 10 || rect.height < 10) continue;
            const rows = t.rows.length;
            let cols = 0;
            for (const r of t.rows) cols = Math.max(cols, r.cells.length);
            const cells = rows * cols;
            if (!best || cells > best.cells) best = {rows, cols, cells};
        } catch (e) {}
    }
    return best;
}"""

RW_TABLE_SCRAPE_JS = """() => {
    let best = null, bestN = -1;
    for (const t of document.querySelectorAll('table')) {
        try {
            const rect = t.getBoundingClientRect();
            if (rect.width < 10 || rect.height < 10) continue;
            const rows = t.rows.length;
            let cols = 0;
            for (const r of t.rows) cols = Math.max(cols, r.cells.length);
            const cells = rows * cols;
            if (cells > bestN) { bestN = cells; best = t; }
        } catch (e) {}
    }
    if (!best || best.rows.length < 2) return null;
    // 헤더행 판정: 첫 행 셀이 th이거나, 두 번째 행과 타입이 다르면(텍스트 vs 숫자) 첫 행이 헤더.
    // VI 크로스탭은 헤더가 th가 아니라 빈 td라서 전부 빈 문자열이 나옴 -> null 반환.
    const firstCells = [...best.rows[0].cells];
    const allTh = firstCells.length > 0 && firstCells.every(c => c.tagName === 'TH');
    const firstTexts = firstCells.map(c => (c.innerText || '').trim());
    const nonEmpty = firstTexts.filter(t => t).length;
    if (!allTh && nonEmpty < Math.max(2, firstCells.length / 2)) return {noHeader: true};
    const headers = firstCells.map(c => (c.innerText || '').trim());
    const rows = [...best.rows].slice(1, 21).map(r =>
        [...r.cells].map(c => (c.innerText || '').trim().slice(0, 40)));
    return {headers, rows, total_rows: Math.max(0, best.rows.length - 1)};
}"""


def load_progress():
    if PROGRESS_FILE.exists():
        try:
            return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_progress(progress):
    DATA_COLUMNS_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(progress, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(PROGRESS_FILE)


def classify_column(col_name, sample_vals, is_numeric):
    name = col_name.strip()
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


def parse_excel_file(excel_path, report_id, report_name, official_id):
    wb = openpyxl.load_workbook(str(excel_path), data_only=True)
    ws = wb.active

    header_row = None
    for r in range(1, min(15, ws.max_row + 1)):
        row_vals = [ws.cell(row=r, column=c).value for c in range(1, min(50, ws.max_column + 1))]
        non_empty = [v for v in row_vals if v is not None and str(v).strip()]
        if len(non_empty) >= 5 and not str(non_empty[0]).startswith("검색조건"):
            header_row = r
            break

    if not header_row:
        header_row = 5

    prompt_info = ws.cell(row=1, column=1).value or ""
    full_report_title = ws.cell(row=3, column=1).value or ""

    columns = []
    for c in range(1, ws.max_column + 1):
        col_name = ws.cell(row=header_row, column=c).value
        if col_name is None or not str(col_name).strip():
            continue
        col_name = str(col_name).strip()

        sample_vals = []
        for r in range(header_row + 1, min(header_row + 21, ws.max_row + 1)):
            v = ws.cell(row=r, column=c).value
            if v is not None:
                sample_vals.append(v)

        is_numeric = all(isinstance(v, (int, float)) for v in sample_vals) if sample_vals else False
        category = classify_column(col_name, sample_vals, is_numeric)
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
        "metrics_summary": {"count": len(metrics), "metrics": metrics},
        "dimensions_summary": {"count": len(dimensions), "dimensions": dimensions},
        "identifiers_summary": {"count": len(identifiers), "identifiers": identifiers},
        "dates_summary": {"count": len(dates), "dates": dates},
        "columns": columns
    }


def parse_columns_from_task_json(text):
    """C경로: taskProc 응답에서 attribute(t:12) 컬럼명 추출 (등장순, 중복제거)."""
    attrs = re.findall(r'"t":12,"st":\d+,"n":"([^"]+)"', text)
    ordered = list(dict.fromkeys(attrs))
    mets = re.findall(r'"t":7,[^}]*?"n":"([^"]+)"', text)
    mets = list(dict.fromkeys(mets))
    return ordered, mets


def build_meta_from_task_json(report_id, report_name, official_id, raw_text):
    """C경로 산출물: 엑셀 파싱과 동일 스키마(행수 -1, 샘플 없음)."""
    cols, mets = parse_columns_from_task_json(raw_text)
    columns = []
    for i, name in enumerate(cols):
        category = classify_column(name, [], False)
        columns.append({
            "index": i + 1,
            "name": name,
            "category": category,
            "is_metric": category == "metric",
            "is_numeric": False,
            "sample_values": []
        })
    metrics = [c["name"] for c in columns if c["is_metric"]]
    dimensions = [c["name"] for c in columns if c["category"] == "dimension"]
    identifiers = [c["name"] for c in columns if c["category"] == "identifier"]
    dates = [c["name"] for c in columns if c["category"] == "date"]
    return {
        "report_id": report_id,
        "report_name": report_name,
        "official_id": official_id,
        "full_report_title": "",
        "prompt_summary": "",
        "sheet_name": "mstr_taskproc_json",
        "source": "MSTR_TASKPROC_JSON",
        "export_path": "mstr-taskProc-json (엑셀버튼 미노출 우회)",
        "total_data_rows": -1,
        "total_columns": len(columns),
        "header_row_index": -1,
        "metrics_summary": {"count": len(metrics), "metrics": metrics},
        "dimensions_summary": {"count": len(dimensions), "dimensions": dimensions},
        "identifiers_summary": {"count": len(identifiers), "identifiers": identifiers},
        "dates_summary": {"count": len(dates), "dates": dates},
        "columns": columns
    }


def _is_number(s):
    try:
        float(str(s).replace(",", "").replace("%", ""))
        return True
    except Exception:
        return False


def build_meta_from_dom(report_id, report_name, official_id, scraped):
    """대시보드 DOM에서 스크랩한 테이블로 엑셀 파싱과 동일 스키마의 메타데이터 생성."""
    headers, rows = scraped["headers"], scraped["rows"]
    columns = []
    for i, h in enumerate(headers):
        name = (h or f"컬럼{i+1}").strip()
        samples = [r[i] for r in rows if i < len(r) and r[i]]
        is_num = bool(samples) and all(_is_number(s) for s in samples)
        category = classify_column(name, samples, is_num)
        columns.append({
            "index": i + 1,
            "name": name,
            "category": category,
            "is_metric": category == "metric",
            "is_numeric": is_num,
            "sample_values": samples[:3]
        })
    metrics = [c["name"] for c in columns if c["is_metric"]]
    dimensions = [c["name"] for c in columns if c["category"] == "dimension"]
    identifiers = [c["name"] for c in columns if c["category"] == "identifier"]
    dates = [c["name"] for c in columns if c["category"] == "date"]
    return {
        "report_id": report_id,
        "report_name": report_name,
        "official_id": official_id,
        "full_report_title": "",
        "prompt_summary": "",
        "sheet_name": "dashboard_dom",
        "source": "RW_DASHBOARD_DOM",
        "total_data_rows": scraped.get("total_rows", 0),
        "total_columns": len(columns),
        "header_row_index": 1,
        "metrics_summary": {"count": len(metrics), "metrics": metrics},
        "dimensions_summary": {"count": len(dimensions), "dimensions": dimensions},
        "identifiers_summary": {"count": len(identifiers), "identifiers": identifiers},
        "dates_summary": {"count": len(dates), "dates": dates},
        "columns": columns
    }


async def process_single_report(browser, report, dry_run=False, label=""):
    report_id = str(report.get("hubReptNo(reptId)")).strip()
    report_name = report.get("보고서명")
    official_id = report.get("보고서ID")

    print(f"\n{label} [{report_id}] {report_name} ({official_id}) 시작...")
    alerts = []

    async def on_dialog(d):
        alerts.append(d.message)
        try:
            await d.accept()
        except Exception:
            pass

    # 독립 컨텍스트 생성 (메모리 격리)
    context = await browser.new_context(
        user_agent=UA,
        viewport={"width": 1440, "height": 900},
        accept_downloads=True
    )

    try:
        pg = await context.new_page()
        pg.on("dialog", on_dialog)

        # 1. 목록 페이지 접근
        url = f"https://data.g2b.go.kr/link/AISC001_01/?reptNm={official_id}"
        await pg.goto(url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
        await pg.wait_for_timeout(2000)

        # 2. 팝업 오픈
        title_sel = "#mf_wfm_container_reptLst_0_reptNm"
        try:
            await pg.wait_for_selector(title_sel, timeout=10000)
        except Exception:
            title_sel = "a[id*='reptLst_0_reptNm']"

        async with context.expect_page(timeout=EXPORT_WINDOW_TIMEOUT_MS) as p_info:
            await pg.click(title_sel)
        popup = await p_info.value
        popup.on("dialog", on_dialog)
        # C경로용: MSTR taskProc(JSON) 응답 후킹 — 엑셀버튼 미노출 시 폴백 재료
        taskproc_bodies = []

        async def on_response(resp):
            try:
                if "taskProc" in resp.url:
                    taskproc_bodies.append(await resp.body())
            except Exception:
                pass

        popup.on("response", on_response)
        try:
            await popup.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
        except Exception:
            pass
        await popup.wait_for_timeout(2000)

        # 2.5 Tier 2: 빈 날짜/년월/셀렉트 조건 자동 주입 (값이 있으면 무해)
        try:
            filled = await popup.evaluate(FILL_DEFAULTS_JS)
            if filled:
                print(f"{label}   🔧 빈 조건 자동 주입: {filled}")
                await popup.wait_for_timeout(500)
        except Exception as e:
            print(f"{label}   🔧 Tier2 evaluate 실패: {str(e)[:120]}")

        # 2.6 Tier 3: 코드팝업 seed 주입 (수동 수집값. 빈 인풋만, 기존값 무해)
        try:
            seed_filled = await popup.evaluate(FILL_SEED_JS, SEED_CODES)
            if seed_filled:
                print(f"{label}   🌱 seed 주입: {seed_filled}")
                await popup.wait_for_timeout(500)
            else:
                print(f"{label}   🌱 seed 매칭 없음 (인풋 0개 or 라벨 불일치)")
        except Exception as e:
            print(f"{label}   🌱 seed evaluate 실패: {str(e)[:120]}")
        # 3. 검색 버튼 클릭
        search_btn = "#mf_popupCnts_btnS0001"
        try:
            await popup.wait_for_selector(search_btn, timeout=15000)
            await popup.click(search_btn)
        except Exception as e:
            return {"status": "FAIL_NO_SEARCH_BTN", "error": str(e)}

        # Alert 발생 여부 짧게 대기 (기록만, 즉시 반환하지 않음.
        # C실험: ALERT와 엑셀 버튼이 공존하므로 엑셀 우선 시도).
        await popup.wait_for_timeout(2500)
        if alerts:
            print(f"{label}   ⚠️ Alert 발생 (계속 진행): {' | '.join(alerts)[:100]}")

        # 4. 조회 완료 대기 (엑셀 버튼 활성화) + RW 대시보드 테이블 렌더 병행 감시
        # ALERT 시 즉시 반환: C실험에서 20초 후 ALERT+엑셀동시 상태가 60초까지
        # 유지되므로 180초 대기는 시간 낭비. ALERT 나면 엑셀 파일부터 받는다.
        excel_btn = "#mf_popupCnts_btnExcelDown:not(.hide)"
        print(f"{label}   조회 완료 대기 중 (ALERT 즉시 반환, 최대 180초)...")
        mstr_fr = None
        excel_active = False
        deadline = time.monotonic() + SEARCH_TIMEOUT_MS / 1000
        while time.monotonic() < deadline:
            # 엑셀 버튼 우선: ALERT와 동시 상태(C실험)에서도 파일은 받을 수 있음.
            # ALERT 반환은 루프 종료 후 엑셀/DOM/JSON 전부 실패 시에만.
            try:
                btn = await popup.query_selector(excel_btn)
                if btn:
                    excel_active = True
                    if alerts:
                        print(f"{label}   ⚠️ ALERT 있지만 엑셀 활성: {' | '.join(alerts)[:80]} -> 다운로드 시도")
                    break
            except Exception:
                pass
            # RW 대시보드: mstrFrame에 테이블 렌더되면 완료로 간주 (폴백 경로)
            # NOTE: Frame API에는 is_closed()가 없고 is_detached()만 있음 (Page에만 is_closed)
            try:
                if mstr_fr is not None and mstr_fr.is_detached():
                    mstr_fr = None
            except Exception:
                mstr_fr = None
            if mstr_fr is None:
                mstr_fr = next((f for f in popup.frames if f.name == "mstrFrame" and f.url != "about:blank"), None)
            if mstr_fr:
                try:
                    probe = await mstr_fr.evaluate(RW_TABLE_PROBE_JS)
                    if probe and probe.get("rows", 0) >= 2 and probe.get("cols", 0) >= 2:
                        print(f"{label}   📊 RW 대시보드 테이블 감지 ({probe['rows']}x{probe['cols']}) -> DOM 폴백")
                        excel_active = False
                        break
                except Exception:
                    pass
            await popup.wait_for_timeout(2000)

        if not excel_active:
            # 폴백: 대시보드 테이블 스크랩
            if mstr_fr:
                try:
                    probe = await mstr_fr.evaluate(RW_TABLE_PROBE_JS)
                except Exception:
                    probe = None
                if probe and probe.get("rows", 0) >= 2 and probe.get("cols", 0) >= 2:
                    scraped = await mstr_fr.evaluate(RW_TABLE_SCRAPE_JS)
                    if scraped and scraped.get("noHeader"):
                        print(f"{label}   ⏭️ DOM 테이블 헤더 없음(VI 크로스탭) -> taskProc 컬럼명으로")
                        scraped = None
                    if scraped and scraped["headers"]:
                        meta = build_meta_from_dom(report_id, report_name, official_id, scraped)
                        out_json = DATA_COLUMNS_DIR / f"{report_id}_columns.json"
                        out_json.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                        print(f"{label}   ✅ [DOM 폴백] 컬럼 {meta['total_columns']}개 "
                              f"(지표 {meta['metrics_summary']['count']}개, 데이터 {meta['total_data_rows']}건)")
                        return {
                            "status": "SUCCESS",
                            "source": "RW_DASHBOARD_DOM",
                            "columns_count": meta["total_columns"],
                            "metrics_count": meta["metrics_summary"]["count"],
                            "data_rows": meta["total_data_rows"],
                            "metrics": meta["metrics_summary"]["metrics"]
                        }
            if alerts:
                return {"status": "ALERT", "error": " | ".join(alerts)}
            # C경로: taskProc JSON 직독 (엑셀버튼 미노출 + DOM 테이블도 없을 때)
            cands = []
            for b in taskproc_bodies:
                try:
                    t = b.decode("utf-8", errors="ignore")
                    if '"t":12' in t:
                        cands.append(t)
                except Exception:
                    pass
            if cands:
                raw = max(cands, key=len)
                try:
                    RAW_JSON_DIR.mkdir(parents=True, exist_ok=True)
                    (RAW_JSON_DIR / f"{report_id}_taskProc.json").write_text(raw, encoding="utf-8")
                except Exception:
                    pass
                meta = build_meta_from_task_json(report_id, report_name, official_id, raw)
                if meta["total_columns"] > 0:
                    out_json = DATA_COLUMNS_DIR / f"{report_id}_columns.json"
                    out_json.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"{label}   ✅ [JSON 폴백] 컬럼 {meta['total_columns']}개 (taskProc 직독)")
                    return {
                        "status": "SUCCESS",
                        "source": "MSTR_TASKPROC_JSON",
                        "columns_count": meta["total_columns"],
                        "metrics_count": meta["metrics_summary"]["count"],
                        "data_rows": -1,
                        "metrics": meta["metrics_summary"]["metrics"]
                    }
            return {"status": "TIMEOUT_SEARCH", "error": "180초 내 검색 완료 신호 미감지 (조회 지연 또는 0건)"}

        if dry_run:
            print(f"{label}   ✅ [Dry-Run] 조회 성공 (엑셀 버튼 활성화 확인 완료)")
            return {"status": "DRY_RUN_OK", "note": "검색 및 엑셀 활성화 확인됨"}

        # 5. MSTR 내보내기: A(핸들러 직접호출) 시도 → 실패 시 B(실버튼 클릭→새탭) 폴백
        export_page = None
        export_source = "EXCEL_A"
        try:
            async with context.expect_page(timeout=EXPORT_WINDOW_TIMEOUT_MS) as opt_info:
                await popup.evaluate("""() => {
                    const cntsComp = $p.getComponentById('mf_popupCnts');
                    const win = cntsComp.getWindow();
                    win.scwin.btnExcelDown_onclick();
                }""")
            export_page = await opt_info.value
        except Exception as e_a:
            print(f"{label}   ⚠️ A경로 실패({str(e_a).split(chr(10))[0][:80]}) → B경로(실버튼 클릭) 시도")
            try:
                async with context.expect_page(timeout=EXPORT_WINDOW_TIMEOUT_MS) as opt_info_b:
                    # 숫자 id 셀렉터 주의: input#3131 불가, #mf_popupCnts_btnExcelDown 은 정상
                    await popup.click("#mf_popupCnts_btnExcelDown")
                export_page = await opt_info_b.value
                export_source = "EXCEL_B"
            except Exception as e_b:
                return {"status": "FAIL_NO_EXPORT_WINDOW",
                        "error": f"A:{str(e_a).split(chr(10))[0][:60]} / B:{str(e_b).split(chr(10))[0][:60]}"}
        export_page.on("dialog", on_dialog)
        try:
            await export_page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
        except Exception:
            pass
        await export_page.wait_for_timeout(1500)

        # 6. 내보내기 버튼(id=3131) 클릭 및 다운로드
        async with export_page.expect_download(timeout=DOWNLOAD_TIMEOUT_MS) as dl_info:
            await export_page.click('input[id="3131"]')

        download = await dl_info.value
        filename = download.suggested_filename
        excel_path = RAW_EXCEL_DIR / f"{report_id}_{filename}"
        await download.save_as(str(excel_path))

        # 7. 메타데이터 파싱
        meta = parse_excel_file(excel_path, report_id, report_name, official_id)
        out_json = DATA_COLUMNS_DIR / f"{report_id}_columns.json"
        out_json.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"{label}   ✅ 수집 성공! [{export_source}] 컬럼 {meta['total_columns']}개 "
              f"(지표 {meta['metrics_summary']['count']}개, 데이터 {meta['total_data_rows']}건)")
        return {
            "status": "SUCCESS",
            "source": export_source,
            "columns_count": meta["total_columns"],
            "metrics_count": meta["metrics_summary"]["count"],
            "data_rows": meta["total_data_rows"],
            "excel_file": excel_path.name,
            "metrics": meta["metrics_summary"]["metrics"]
        }

    except Exception as e:
        err_msg = str(e).split("\n")[0][:150]
        if alerts:
            err_msg = f"{err_msg} (Alerts: {' | '.join(alerts)})"
        print(f"{label}   ❌ 에러 발생: {err_msg}")
        return {"status": "FAIL", "error": err_msg}

    finally:
        try:
            await context.close()
        except Exception:
            pass


async def run_batch(limit=None, dry_run=False, force=False, start_idx=0, workers=5):
    from playwright.async_api import async_playwright

    reports = json.loads(REPORTS_PATH.read_text(encoding="utf-8"))
    reports = reports[start_idx:start_idx + limit] if limit else reports[start_idx:]

    progress = load_progress()
    print("=== 🚀 조달데이터허브 보고서 메타데이터 배치 시작 (비동기 병렬) ===")
    print(f"대상: {len(reports)}건 | 동시 실행: {workers}건 | Dry-Run: {dry_run} | 기존 완료 스킵: {not force}")

    RAW_EXCEL_DIR.mkdir(parents=True, exist_ok=True)
    DATA_COLUMNS_DIR.mkdir(parents=True, exist_ok=True)

    stats = {}
    lock = asyncio.Lock()

    def is_done(rid):
        # 실제 산출물(columns.json)이 있거나 SUCCESS 기록이 있으면 완료로 간주
        if (DATA_COLUMNS_DIR / f"{rid}_columns.json").exists():
            return True
        entry = progress.get(rid)
        return bool(entry and entry.get("status") == "SUCCESS")

    # 기존 결과물 있는 건 사전 스킵
    pending = []
    for i, report in enumerate(reports):
        rid = str(report.get("hubReptNo(reptId)")).strip()
        name = report.get("보고서명")
        if not force and is_done(rid):
            print(f"[{i+1}/{len(reports)}] [{rid}] {name} -> 기존 결과물 있어 스킵")
            stats["SKIPPED"] = stats.get("SKIPPED", 0) + 1
            continue
        pending.append((i, report))

    print(f"실행 대상: {len(pending)}건 | 스킵: {stats.get('SKIPPED', 0)}건")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        sem = asyncio.Semaphore(workers)

        async def runner(i, report):
            rid = str(report.get("hubReptNo(reptId)")).strip()
            label = f"[{i+1}/{len(reports)}]"
            # 사이트 부하 분산을 위한 지터
            await asyncio.sleep(random.uniform(0.5, 3.0))
            async with sem:
                try:
                    res = await process_single_report(browser, report, dry_run=dry_run, label=label)
                except Exception as e:
                    res = {"status": "FAIL", "error": str(e).split("\n")[0][:150]}

            res["report_name"] = report.get("보고서명")
            res["official_id"] = report.get("보고서ID")
            res["mstrFrmt"] = report.get("mstrFrmt")
            res["updated_at"] = datetime.now().isoformat()

            async with lock:
                progress[rid] = res
                save_progress(progress)
                stats[res["status"]] = stats.get(res["status"], 0) + 1
                if res.get("status") == "SUCCESS":
                    stats[f"SUCCESS:{res.get('source', '?')}"] = stats.get(f"SUCCESS:{res.get('source', '?')}", 0) + 1
            # 짧은 휴식 (차단 회피)
            await asyncio.sleep(0.5)

        if pending:
            await asyncio.gather(*(runner(i, r) for i, r in pending))
        await browser.close()

    print("\n" + "=" * 60)
    print("📊 배치 실행 결과 요약")
    print("=" * 60)
    for k, v in stats.items():
        if v > 0:
            print(f"  - {k:<18}: {v}건")
    print(f"진행상황 저장: {PROGRESS_FILE}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="보고서 출력 메타데이터 배치 수집기 (비동기 병렬)")
    parser.add_argument("--limit", type=int, default=None, help="실행할 보고서 수 (기본: 전체)")
    parser.add_argument("--start-idx", type=int, default=0, help="시작 인덱스 (기본: 0)")
    parser.add_argument("--workers", type=int, default=5, help="동시 실행 워커 수 (기본: 5)")
    parser.add_argument("--dry-run", action="store_true", help="엑셀 다운로드 없이 조회 성공 여부만 확인")
    parser.add_argument("--force", action="store_true", help="기존 완료 건도 재실행")
    args = parser.parse_args()

    asyncio.run(run_batch(
        limit=args.limit,
        dry_run=args.dry_run,
        force=args.force,
        start_idx=args.start_idx,
        workers=args.workers,
    ))


if __name__ == "__main__":
    main()
