#!/usr/bin/env python3
"""mstrFrame body children 구조 덤프 - 검색 후 실제로 뭘 렌더링했는지 확인."""
import asyncio
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

DUMP_JS = """() => {
    const describe = (el, depth) => {
        if (depth > 4) return null;
        const item = {
            tag: el.tagName,
            id: (el.id || '').slice(0, 60),
            cls: String(el.className || '').slice(0, 80),
            text: (el.childElementCount === 0 ? (el.textContent || '').trim().slice(0, 100) : ''),
            children: []
        };
        [...el.children].slice(0, 12).forEach(c => {
            const d = describe(c, depth + 1);
            if (d) item.children.push(d);
        });
        return item;
    };
    return {
        url: location.href.slice(0, 200),
        readyState: document.readyState,
        htmlLen: document.documentElement.outerHTML.length,
        body: describe(document.body, 0),
        allIframes: [...document.querySelectorAll('iframe')].map(f => ({id: f.id, src: (f.src||'').slice(0, 120)})),
        hiddenEls: [...document.querySelectorAll('[style*="display: none"], [style*="display:none"]')].length
    };
}"""


async def main():
    from playwright.async_api import async_playwright
    rid = sys.argv[1] if len(sys.argv) > 1 else "00197"
    reports = json.loads((BASE_DIR / "public" / "reports131.json").read_text(encoding="utf-8"))
    report = next(r for r in reports if str(r.get("hubReptNo(reptId)")).strip() == rid)
    official_id = report.get("보고서ID")
    print(f"대상: [{rid}] {report.get('보고서명')}")

    async with async_playwright() as p:
        launch_kw = {"headless": True, "args": ["--no-sandbox", "--disable-setuid-sandbox"]}
        if len(sys.argv) > 2 and sys.argv[2] == "new":
            launch_kw["channel"] = "chromium"
            print(">> 신규 헤드리스(channel=chromium) 모드 테스트")
        browser = await p.chromium.launch(**launch_kw)
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
        await popup.evaluate("""() => {
            const c = $p.getComponentById('mf_popupCnts');
            c.getWindow().scwin.btnS0001_onclick();
        }""")
        await popup.wait_for_timeout(35000)

        mstr_fr = next((f for f in popup.frames if f.name == "mstrFrame"), None)
        if mstr_fr:
            try:
                dump = await mstr_fr.evaluate(DUMP_JS)
                out = BASE_DIR / "data" / "columns" / f"mstr_dump_{rid}.json"
                out.write_text(json.dumps(dump, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"url: {dump['url']}")
                print(f"readyState={dump['readyState']} htmlLen={dump['htmlLen']} hidden={dump['hiddenEls']}")
                print(f"iframes: {dump['allIframes']}")
                print(f"body tag={dump['body']['tag']} children={len(dump['body']['children'])}")
                def pr(node, ind=0):
                    t = f"{'  '*ind}<{node['tag']}> id={node['id']!r} cls={node['cls'][:50]!r}"
                    if node['text']:
                        t += f" text={node['text'][:60]!r}"
                    print(t)
                    for c in node['children']:
                        pr(c, ind+1)
                pr(dump['body'])
                print(f"저장: {out}")
            except Exception as e:
                print(f"덤프 실패: {str(e)[:100]}")
        else:
            print("mstrFrame 없음")
        await browser.close()

asyncio.run(main())
