#!/usr/bin/env python3
"""mstrWeb 관련 요청/응답 상세 추적 - VI 대시보드 데이터 페치 실패 지점 파악."""
import asyncio
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


async def main():
    from playwright.async_api import async_playwright
    rid = sys.argv[1] if len(sys.argv) > 1 else "00197"
    wait_s = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    reports = json.loads((BASE_DIR / "public" / "reports131.json").read_text(encoding="utf-8"))
    report = next(r for r in reports if str(r.get("hubReptNo(reptId)")).strip() == rid)
    official_id = report.get("보고서ID")
    print(f"대상: [{rid}] {report.get('보고서명')} | 대기 {wait_s}초")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = await browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900})
        pg = await context.new_page()
        await pg.goto(f"https://data.g2b.go.kr/link/AISC001_01/?reptNm={official_id}",
                      wait_until="networkidle", timeout=60000)
        await pg.wait_for_timeout(2000)
        title_sel = "#mf_wfm_container_reptLst_0_reptNm"
        try:
            await pg.wait_for_selector(title_sel, timeout=10000)
        except Exception:
            title_sel = "a[id*='reptLst_0_reptNm']"
        async with context.expect_page(timeout=60000) as p_info:
            await pg.click(title_sel)
        popup = await p_info.value
        try:
            await popup.wait_for_load_state("networkidle", timeout=60000)
        except Exception:
            pass
        await popup.wait_for_timeout(3000)

        entries = []

        def on_response(resp):
            u = resp.url
            if any(k in u for k in ("mstrWeb", "taskProc", "servlet", ".aspx", "json")) and \
               not any(k in u for k in (".js", ".css", ".png", ".gif", ".woff", ".svg", ".jpg")):
                entries.append(("RESP", resp.status, resp.request.method, u[:140]))

        def on_reqfail(req):
            u = req.url
            if "mstrWeb" in u or "taskProc" in u:
                entries.append(("FAIL", req.failure, req.method, u[:140]))

        popup.on("response", on_response)
        popup.on("requestfailed", on_reqfail)

        await popup.evaluate("""() => {
            const c = $p.getComponentById('mf_popupCnts');
            c.getWindow().scwin.btnS0001_onclick();
        }""")
        print("[검색 실행]")

        for t in range(20, wait_s + 1, 20):
            await popup.wait_for_timeout(20000)
            vidoc = None
            mstr_fr = next((f for f in popup.frames if f.name == "mstrFrame"), None)
            if mstr_fr:
                try:
                    vidoc = await mstr_fr.evaluate(
                        "() => { const d = document.getElementById('mstr56');"
                        " return d ? d.childElementCount : 'noDoc'; }")
                except Exception:
                    vidoc = "evalErr"
            excel_btn = await popup.evaluate(
                "() => { const b = document.querySelector('#mf_popupCnts_btnExcelDown');"
                " return b ? !b.className.includes('hide') : null; }")
            print(f"[{t}초] VIDocument children={vidoc} excelBtn={excel_btn} 신규응답={len(entries)}")

        print("\n=== mstrWeb/taskProc 관련 응답 목록 ===")
        for e in entries[:40]:
            print(e)

        await browser.close()

asyncio.run(main())
