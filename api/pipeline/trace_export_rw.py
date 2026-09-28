#!/usr/bin/env python3
"""RW(대시보드)형 보고서에서 엑셀 export 핸들러 강제 호출 테스트.
btnExcelDown_onclick()이 숨김 상태에서도 동작하는지 확인."""
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
    print(f"대상: [{rid}] {report.get('보고서명')} ({official_id})")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = await browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900},
                                            accept_downloads=True)
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

        await popup.evaluate("""() => {
            const c = $p.getComponentById('mf_popupCnts');
            c.getWindow().scwin.btnS0001_onclick();
        }""")
        print("[검색 실행] VI 렌더 대기 40초...")
        await popup.wait_for_timeout(40000)

        # VI 콘텐츠 렌더 확인
        mstr_fr = next((f for f in popup.frames if f.name == "mstrFrame"), None)
        if mstr_fr:
            n = await mstr_fr.evaluate("() => document.querySelectorAll('table').length")
            print(f"mstrFrame 테이블 수: {n}")

        # export 핸들러 강제 호출 (버튼 숨김 상태 무시)
        try:
            async with context.expect_page(timeout=30000) as opt_info:
                await popup.evaluate("""() => {
                    const c = $p.getComponentById('mf_popupCnts');
                    c.getWindow().scwin.btnExcelDown_onclick();
                }""")
            export_page = await opt_info.value
            print(f">>> export 페이지 오픈 성공: {export_page.url[:100]}")
            try:
                await export_page.wait_for_load_state("networkidle", timeout=30000)
            except Exception:
                pass
            await export_page.wait_for_timeout(2000)

            info = await export_page.evaluate("""() => ({
                title: document.title,
                inputs: [...document.querySelectorAll('input')].map(i => ({id: i.id, type: i.type, value: i.value})).slice(0, 20),
                bodyText: document.body ? document.body.innerText.replace(/\\s+/g, ' ').slice(0, 300) : ''
            })""")
            print(f"title: {info['title']}")
            print(f"bodyText: {info['bodyText'][:200]}")
            for i in info['inputs']:
                print(f"  input id={i['id']!r} type={i['type']} value={i['value']!r}")

            # id=3131 클릭 다운로드 시도
            has3131 = await export_page.evaluate("() => !!document.getElementById('3131')")
            if has3131:
                async with export_page.expect_download(timeout=60000) as dl_info:
                    await export_page.click('input[id="3131"]')
                dl = await dl_info.value
                out = BASE_DIR / "data" / "raw_excel" / f"{rid}_TEST_{dl.suggested_filename}"
                await dl.save_as(str(out))
                print(f">>> 다운로드 성공: {out.name} ({out.stat().st_size} bytes)")
            else:
                print(">>> id=3131 없음 (export 옵션 페이지 구조 다름)")
                await export_page.screenshot(path=str(BASE_DIR / "data" / "columns" / f"export_page_{rid}.png"))
        except Exception as e:
            print(f">>> export 핸들러 호출 실패: {str(e)[:200]}")

        await browser.close()

asyncio.run(main())
