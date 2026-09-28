#!/usr/bin/env python3
"""검색 클릭 후 네트워크 요청/프레임 로딩 추적 - 실패(통계) vs 성공(내역) 보고서 비교."""
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
    reports = json.loads((BASE_DIR / "public" / "reports131.json").read_text(encoding="utf-8"))
    report = next(r for r in reports if str(r.get("hubReptNo(reptId)")).strip() == rid)
    official_id = report.get("보고서ID")
    print(f"추적 대상: [{rid}] {report.get('보고서명')} ({official_id}) mstrFrmt={report.get('mstrFrmt')}")

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

        reqs = []
        popup.on("request", lambda r: reqs.append((r.frame.name[:12] or "main", r.url[:110])))

        await popup.evaluate("""() => {
            const c = $p.getComponentById('mf_popupCnts');
            c.getWindow().scwin.btnS0001_onclick();
        }""")
        print("[검색 실행] 완료")

        for t in range(1, 7):
            await popup.wait_for_timeout(10000)
            frames = [(f.name[:14] or "main", f.url[:90]) for f in popup.frames]
            recent = reqs[-8:]
            print(f"\n[{t*10}초] frames: {frames}")
            print(f"  최근요청: {recent}")

        await browser.close()

asyncio.run(main())
