#!/usr/bin/env python3
"""taskProc/mstrWeb 응답 본문 캡처 - 대시보드 데이터가 실제로 비어있는지/오류인지 확인."""
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
            if ("taskProc" in u or "mstrWeb" in u) and ".js" not in u:
                try:
                    body = await resp.text()
                    bodies.append({"url": u[:100], "status": resp.status,
                                   "len": len(body), "body": body[:3000]})
                except Exception as e:
                    bodies.append({"url": u[:100], "status": resp.status, "err": str(e)[:80]})

        popup.on("response", lambda r: asyncio.ensure_future(on_response(r)))

        await popup.evaluate("""() => {
            const c = $p.getComponentById('mf_popupCnts');
            c.getWindow().scwin.btnS0001_onclick();
        }""")
        await popup.wait_for_timeout(45000)

        print(f"캡처된 응답: {len(bodies)}건\n")
        for i, b in enumerate(bodies):
            print(f"--- 응답 {i+1}: {b['url']} status={b['status']} len={b.get('len')} ---")
            body = b.get('body', '')
            # 핵심 키워드 추출
            for kw in ('errorCode', 'error', 'Error', '오류', '없습니다', 'no data', 'items',
                       'data', 'rows', 'totalCount', 'message'):
                idx = body.find(kw)
                if idx >= 0:
                    print(f"  [{kw}] ...{body[max(0,idx-60):idx+120]}...")
                    break
            # 앞부분 원문 출력
            print(f"  RAW: {body[:500]}")
            print()

        out = BASE_DIR / "data" / "columns" / f"resp_bodies_{rid}.json"
        out.write_text(json.dumps(bodies, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"저장: {out}")
        await browser.close()

asyncio.run(main())
