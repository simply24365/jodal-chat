#!/usr/bin/env python3
"""00030 v2 진단: mstrFrame 내부 + 1개월 범위 재검색 + 도움말 팝업. 신규 파일."""
import argparse, json
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent
REPORTS_PATH = BASE_DIR / "public" / "reports131.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

def frame_state(popup):
    try:
        return popup.evaluate("""() => {
          const f = document.querySelector('iframe#mstrFrame');
          if (!f) return {noframe: true};
          let inner = '';
          try {
            const d = f.contentDocument;
            inner = d ? (d.body ? d.body.innerText.slice(0, 600) : 'no-body') : 'no-doc';
          } catch(e) { inner = 'CROSS_ORIGIN:' + (f.src||'').slice(0,120); }
          const bar = document.querySelector('#__processbarIFrame');
          return {src: (f.src||'').slice(0,150), inner, barVisible: bar ? bar.offsetParent !== null : 'no-bar'};
        }""")
    except Exception as e:
        return f"EVAL_FAIL {e}"

def btn_state(popup):
    try:
        return popup.evaluate("""() => {
          const b = document.querySelector('#mf_popupCnts_btnExcelDown');
          return b ? b.className : 'NO_BTN';
        }""")
    except Exception as e:
        return f"EVAL_FAIL {e}"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-id", default="00030")
    a = ap.parse_args()
    from playwright.sync_api import sync_playwright
    reports = json.loads(REPORTS_PATH.read_text(encoding="utf-8"))
    r = next(x for x in reports if str(x.get("hubReptNo(reptId)")).strip() == a.report_id)
    official_id = r.get("보고서ID")
    alerts = []
    with sync_playwright() as p:
        bw = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        ctx = bw.new_context(user_agent=UA, viewport={"width": 1440, "height": 900}, accept_downloads=True)
        pg = ctx.new_page()
        pg.on("dialog", lambda d: (alerts.append(f"L:{d.message}"), d.accept()))
        pg.goto(f"https://data.g2b.go.kr/link/AISC001_01/?reptNm={official_id}", wait_until="networkidle", timeout=60_000)
        pg.wait_for_timeout(2000)
        sel = "#mf_wfm_container_reptLst_0_reptNm"
        try: pg.wait_for_selector(sel, timeout=10_000)
        except Exception: sel = "a[id*='reptLst_0_reptNm']"
        with ctx.expect_page(timeout=60_000) as pi: pg.click(sel)
        popup = pi.value
        popup.on("dialog", lambda d: (alerts.append(f"P:{d.message}"), d.accept()))
        try: popup.wait_for_load_state("networkidle", timeout=60_000)
        except Exception: pass
        popup.wait_for_timeout(2000)

        print(f"[1] 검색전 frame={json.dumps(frame_state(popup), ensure_ascii=False)[:700]}", flush=True)
        popup.wait_for_selector("#mf_popupCnts_btnS0001", timeout=15_000)
        popup.click("#mf_popupCnts_btnS0001")
        popup.wait_for_timeout(15000)
        print(f"[2] 검색+15s btn={btn_state(popup)} alerts={alerts}", flush=True)
        print(f"    frame={json.dumps(frame_state(popup), ensure_ascii=False)[:700]}", flush=True)

        # 1개월 버튼 탐색 후 클릭 → 재검색
        try:
            btns = popup.evaluate("""() => Array.from(document.querySelectorAll('input[type=button]'))
              .filter(b => /개월/.test(b.value||'')).map(b => ({id: b.id, value: b.value}))""")
            print(f"[3] 개월버튼 목록={btns}", flush=True)
            m1 = popup.query_selector("input[value='1개월']")
            if m1:
                m1.click(); popup.wait_for_timeout(1500)
                dvals = popup.evaluate("""() => ({s: (document.querySelector('[id*=ibxStrDay]')||{}).value||'',
                  e: (document.querySelector('[id*=ibxEndDay]')||{}).value||''})""")
                print(f"[4] 1개월클릭 후 일자={dvals}", flush=True)
                popup.click("#mf_popupCnts_btnS0001")
                for i in range(6):
                    popup.wait_for_timeout(10000)
                    print(f"[5-{i+1}] btn={btn_state(popup)} frame={json.dumps(frame_state(popup), ensure_ascii=False)[:500]}", flush=True)
                    if "hide" not in str(btn_state(popup)): break
        except Exception as e:
            print(f"[개월테스트 실패] {e}", flush=True)

        # 도움말 팝업 열어 대체 내보내기 안내 확인
        try:
            with ctx.expect_page(timeout=20_000) as hi:
                popup.click("#mf_popupCnts_btnReptHelp")
            hlp = hi.value
            try: hlp.wait_for_load_state("networkidle", timeout=20_000)
            except Exception: pass
            hlp.wait_for_timeout(1500)
            txt = hlp.evaluate("() => document.body ? document.body.innerText.slice(0, 1200) : ''")
            print(f"[6] 도움말 본문=\n{txt}", flush=True)
        except Exception as e:
            print(f"[도움말 오픈 실패] {e}", flush=True)
        bw.close()

if __name__ == "__main__":
    main()
