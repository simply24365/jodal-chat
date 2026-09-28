#!/usr/bin/env python3
"""반정형 보고서 조회-대기 구간 on-the-fly 진단기. 새 파일 (기존 스크립트 미수정).

대상: --report-id 00030 (조달요청 내역) 기본.
동작: 목록→팝업→검색클릭 후, N초 간격으로 팝업 상태를 폴링하며 로그 출력.
확인 항목: alert, 엑셀버튼 class/hide, mstrFrame 로딩, 그리드 행수, 프롬프트 입력값.
"""
import argparse
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
REPORTS_PATH = BASE_DIR / "public" / "reports131.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-id", default="00030")
    ap.add_argument("--poll-sec", type=int, default=10)
    ap.add_argument("--poll-count", type=int, default=18)  # 18*10s = 3분
    a = ap.parse_args()

    from playwright.sync_api import sync_playwright
    reports = json.loads(REPORTS_PATH.read_text(encoding="utf-8"))
    r = next(x for x in reports if str(x.get("hubReptNo(reptId)")).strip() == a.report_id)
    official_id = r.get("보고서ID")
    print(f"[*] 대상 [{a.report_id}] {r.get('보고서명')} ({official_id})", flush=True)
    alerts = []
    with sync_playwright() as p:
        bw = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        ctx = bw.new_context(user_agent=UA, viewport={"width": 1440, "height": 900}, accept_downloads=True)
        pg = ctx.new_page()
        pg.on("dialog", lambda d: (alerts.append(f"LIST:{d.message}"), d.accept()))
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
        popup.on("dialog", lambda d: (alerts.append(f"POPUP:{d.message}"), d.accept()))
        try:
            popup.wait_for_load_state("networkidle", timeout=60_000)
        except Exception:
            pass
        popup.wait_for_timeout(2000)
        # 검색 전: 프롬프트 입력값 스냅샷
        try:
            snap = popup.evaluate("""() => {
              const out = {};
              document.querySelectorAll('#mf_popupCnts input, #mf_popupCnts select').forEach(el => {
                if (el.id) out[el.id] = (el.value ?? '').slice(0, 60);
              });
              return out;
            }""")
            print(f"[검색전 inputs] {json.dumps(snap, ensure_ascii=False)[:1500]}", flush=True)
        except Exception as e:
            print(f"[검색전 inputs 실패] {e}", flush=True)
        popup.wait_for_selector("#mf_popupCnts_btnS0001", timeout=15_000)
        popup.click("#mf_popupCnts_btnS0001")
        print("[*] 검색 클릭 → 폴링 시작", flush=True)
        for i in range(a.poll_count):
            popup.wait_for_timeout(a.poll_sec * 1000)
            try:
                st = popup.evaluate("""() => {
                  const b = document.querySelector('#mf_popupCnts_btnExcelDown');
                  const frames = Array.from(document.querySelectorAll('iframe')).map(f => f.id || f.name || f.src?.slice(0,80));
                  const bodyTxt = document.body ? document.body.innerText.slice(0, 300) : '';
                  // mstr 그리드 행 추정
                  const rows = document.querySelectorAll('table tr').length;
                  return {btnClass: b ? b.className : 'NO_BTN',
                          btnVisible: b ? (b.offsetParent !== null) : false,
                          btnValue: b ? b.value : '',
                          iframes: frames, tableRows: rows, bodyHead: bodyTxt};
                }""")
            except Exception as e:
                st = f"EVAL_FAIL: {e}"
            print(f"[{i+1}/{a.poll_count}] alerts={alerts} state={json.dumps(st, ensure_ascii=False)[:800]}", flush=True)
            if isinstance(st, dict) and "hide" not in st.get("btnClass", "") and st.get("btnClass") != "NO_BTN":
                print("[+] 엑셀버튼 활성화 감지! (.hide 제거됨)", flush=True)
                break
        bw.close()


if __name__ == "__main__":
    main()
