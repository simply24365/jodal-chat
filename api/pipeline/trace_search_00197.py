#!/usr/bin/env python3
"""실패 보고서 검색 동작 추적 - 0건 메시지 여부와 엑셀버튼 상태 변화를 실시간 관찰."""
import asyncio
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

STATE_JS = """() => {
    const excelBtn = document.querySelector('#mf_popupCnts_btnExcelDown');
    const cnts = document.querySelector('#mf_popupCnts');
    const txt = cnts ? cnts.innerText.replace(/\\s+/g, ' ').slice(0, 600) : '';
    const alerts = [];
    document.querySelectorAll('.w2alert, [class*="alert"], [id*="alert"]').forEach(el => {
        const t = (el.innerText || '').trim();
        if (t) alerts.push(t.slice(0, 120));
    });
    return {
        excelHide: excelBtn ? excelBtn.className.includes('hide') : null,
        excelCls: excelBtn ? String(excelBtn.className).slice(0, 100) : null,
        alerts: alerts.slice(0, 5),
        cntsText: txt
    };
}"""


async def main():
    from playwright.async_api import async_playwright
    rid = sys.argv[1] if len(sys.argv) > 1 else "00197"
    reports = json.loads((BASE_DIR / "public" / "reports131.json").read_text(encoding="utf-8"))
    report = next(r for r in reports if str(r.get("hubReptNo(reptId)")).strip() == rid)
    official_id = report.get("보고서ID")
    print(f"추적 대상: [{rid}] {report.get('보고서명')} ({official_id})")

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

        st = await popup.evaluate(STATE_JS)
        print(f"[클릭 전] excelHide={st['excelHide']} alerts={st['alerts']}")
        print(f"[클릭 전] cntsText: {st['cntsText'][:300]}")

        # 배치와 동일한 방식으로 검색 실행 (scwin 핸들러 직접 호출)
        await popup.evaluate("""() => {
            const c = $p.getComponentById('mf_popupCnts');
            c.getWindow().scwin.btnS0001_onclick();
        }""")
        print("[검색 실행] btnS0001_onclick 호출 완료")

        for t in range(6, 91, 6):
            await popup.wait_for_timeout(6000)
            try:
                st = await popup.evaluate(STATE_JS)
                print(f"[{t:3d}초] excelHide={st['excelHide']} alerts={st['alerts']} | {st['cntsText'][:160]}")
                if not st["excelHide"]:
                    print(">>> 엑셀버튼 활성화!")
                    break
            except Exception as e:
                print(f"[{t:3d}초] evaluate 오류: {str(e)[:80]}")

        await browser.close()

asyncio.run(main())
