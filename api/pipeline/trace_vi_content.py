#!/usr/bin/env python3
"""VIDocument 내부 콘텐츠 심층 덤프 + 전체 taskProc 응답 저장."""
import asyncio
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

VI_DUMP_JS = """() => {
    const doc = document.getElementById('mstr56');
    const out = { docHtmlLen: doc ? doc.innerHTML.length : -1,
                  docChildClasses: doc ? [...doc.querySelectorAll('*')].slice(0, 60).map(e => e.tagName + '.' + String(e.className).slice(0, 60)) : [],
                  visibleText: doc ? doc.innerText.replace(/\\s+/g, ' ').slice(0, 400) : '',
                  mainAppMsg: (document.getElementById('mainAppMsg') || {}).innerText || '' };
    // 그리드 관련 마커 검색
    const markers = {};
    ['.mstrmojo-Grid', 'table', '[id*="kWG"]', '.mstrmojo-VIGraph', 'svg', 'canvas',
     '.mstrmojo-DataGrid', '[class*="grid"]', '[class*="Grid"]'].forEach(sel => {
        try { markers[sel] = document.querySelectorAll(sel).length; } catch(e) { markers[sel] = 'err'; }
    });
    out.markers = markers;
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

        bodies = []

        async def on_response(resp):
            u = resp.url
            if "taskProc" in u:
                try:
                    body = await resp.text()
                    bodies.append({"url": u, "status": resp.status, "len": len(body), "body": body})
                except Exception as e:
                    bodies.append({"url": u, "status": resp.status, "err": str(e)[:80]})

        popup.on("response", lambda r: asyncio.ensure_future(on_response(r)))

        await popup.evaluate("""() => {
            const c = $p.getComponentById('mf_popupCnts');
            c.getWindow().scwin.btnS0001_onclick();
        }""")
        await popup.wait_for_timeout(50000)

        mstr_fr = next((f for f in popup.frames if f.name == "mstrFrame"), None)
        if mstr_fr:
            try:
                vi = await mstr_fr.evaluate(VI_DUMP_JS)
                print(f"docHtmlLen={vi['docHtmlLen']}")
                print(f"visibleText: {vi['visibleText'][:200]!r}")
                print(f"mainAppMsg: {vi['mainAppMsg'][:200]!r}")
                print("markers:", json.dumps(vi['markers']))
                for c in vi['docChildClasses'][:40]:
                    print("  ", c)
            except Exception as e:
                print(f"VI 덤프 실패: {str(e)[:100]}")

        out = BASE_DIR / "data" / "columns" / f"taskproc_full_{rid}.json"
        out.write_text(json.dumps(bodies, ensure_ascii=False), encoding="utf-8")
        print(f"\ntaskProc 응답 {len(bodies)}건 저장 (전체 본문): {out}")
        for i, b in enumerate(bodies):
            print(f"  [{i+1}] status={b.get('status')} len={b.get('len')}")
        await browser.close()

asyncio.run(main())
