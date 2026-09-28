#!/usr/bin/env python3
"""검색 전/후 팝업 스크린샷 저장 - 통계형 보고서 렌더링 멈춤 지점을 시각적으로 확인."""
import asyncio
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

HTML_LEN_JS = """() => {
    const fr = [];
    window.frames && Object.keys(window.frames).forEach(k => {});
    return {
        docHeight: document.documentElement.scrollHeight,
        bodyChildren: document.body ? document.body.children.length : -1,
        readyState: document.readyState,
        iframes: [...document.querySelectorAll('iframe')].map(f => ({id: f.id, name: f.name, src: (f.src||'').slice(0,100)}))
    };
}"""


async def main():
    from playwright.async_api import async_playwright
    rid = sys.argv[1] if len(sys.argv) > 1 else "00197"
    reports = json.loads((BASE_DIR / "public" / "reports131.json").read_text(encoding="utf-8"))
    report = next(r for r in reports if str(r.get("hubReptNo(reptId)")).strip() == rid)
    official_id = report.get("보고서ID")
    shot_dir = BASE_DIR / "data" / "columns"
    print(f"대상: [{rid}] {report.get('보고서명')} ({official_id})")

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
        await popup.screenshot(path=str(shot_dir / f"shot_{rid}_before.png"))

        mstr_fr = next((f for f in popup.frames if f.name == "mstrFrame"), None)
        if mstr_fr:
            try:
                print("mstrFrame(검색전):", await mstr_fr.evaluate(HTML_LEN_JS))
            except Exception as e:
                print("mstrFrame eval 실패:", str(e)[:80])

        await popup.evaluate("""() => {
            const c = $p.getComponentById('mf_popupCnts');
            c.getWindow().scwin.btnS0001_onclick();
        }""")
        await popup.wait_for_timeout(30000)
        await popup.screenshot(path=str(shot_dir / f"shot_{rid}_after30s.png"))

        mstr_fr = next((f for f in popup.frames if f.name == "mstrFrame"), None)
        if mstr_fr:
            try:
                print("mstrFrame(검색후30s):", await mstr_fr.evaluate(HTML_LEN_JS))
            except Exception as e:
                print("mstrFrame eval 실패:", str(e)[:80])
        print(f"스크린샷 저장: shot_{rid}_before.png / shot_{rid}_after30s.png")
        await browser.close()

asyncio.run(main())
