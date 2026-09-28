#!/usr/bin/env python3
"""mstrFrame 내부 콘솔 에러/렌더링 상태 확인 - 리포트가 실행되지 않는 이유 파악."""
import asyncio
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

MSTR_STATE_JS = """() => {
    const out = {};
    out.title = document.title;
    out.bodyText = (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').slice(0, 500);
    out.bodyHtml = (document.body ? document.body.innerHTML : '').slice(0, 300);
    const grids = document.querySelectorAll('.grid-resize, .mstrReportGrid, table[id*="grid"], div[id*="Grid"]');
    out.gridCount = grids.length;
    out.gridTags = [...grids].slice(0, 5).map(g => g.tagName + '#' + (g.id || '').slice(0, 40));
    // MSTR 전역 상태
    try { out.hasMstr = typeof microstrategy !== 'undefined'; } catch(e) { out.hasMstr = false; }
    try {
        if (out.hasMstr && microstrategy.bones && microstrategy.bones.UniqueReportID) {
            out.uniqueReportMsgID = !!microstrategy.bones.UniqueReportID.messageID;
        }
    } catch(e) { out.urbErr = e.message; }
    return out;
}"""


async def main():
    from playwright.async_api import async_playwright
    rid = sys.argv[1] if len(sys.argv) > 1 else "00197"
    reports = json.loads((BASE_DIR / "public" / "reports131.json").read_text(encoding="utf-8"))
    report = next(r for r in reports if str(r.get("hubReptNo(reptId)")).strip() == rid)
    official_id = report.get("보고서ID")
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

        logs = []
        popup.on("console", lambda m: logs.append(f"[{m.type}] {m.text[:140]}"))
        popup.on("pageerror", lambda e: logs.append(f"[PAGEERROR] {str(e)[:140]}"))

        await popup.evaluate("""() => {
            const c = $p.getComponentById('mf_popupCnts');
            c.getWindow().scwin.btnS0001_onclick();
        }""")
        print("[검색 실행]")

        for t in (15, 30, 50, 70):
            await popup.wait_for_timeout(t - (15 if t == 15 else (30 if t == 30 else (20 if t == 50 else 20))))
            mstr_fr = next((f for f in popup.frames if f.name == "mstrFrame"), None)
            print(f"\n=== [{t}초] ===")
            if mstr_fr:
                try:
                    st = await mstr_fr.evaluate(MSTR_STATE_JS)
                    print(f"  title={st['title']!r} grids={st['gridCount']} {st.get('gridTags')}")
                    print(f"  bodyText: {st['bodyText'][:200]}")
                    print(f"  hasMstr={st['hasMstr']} msgID={st.get('uniqueReportMsgID', 'N/A')}")
                except Exception as e:
                    print(f"  evaluate 실패: {str(e)[:80]}")
            new_logs = logs[-10:]
            for l in new_logs:
                print(f"  LOG: {l}")
            logs = []

        await browser.close()

asyncio.run(main())
