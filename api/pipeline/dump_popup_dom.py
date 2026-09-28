#!/usr/bin/env python3
"""실패 보고서 팝업 프레임별 DOM 덤프 - MSTR iframe 내부 프롬프트 input 확인용."""
import asyncio
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

FRAME_DUMP_JS = """() => {
    const out = { url: location.href.slice(0, 150), inputs: [], selects: [], buttons: [] };
    document.querySelectorAll('input').forEach(el => {
        out.inputs.push({
            id: el.id.slice(0, 80),
            type: el.type,
            value: String(el.value || '').slice(0, 40),
            className: String(el.className || '').slice(0, 100),
            name: el.name || '',
            readOnly: el.readOnly,
            html: el.outerHTML.slice(0, 250)
        });
    });
    document.querySelectorAll('select').forEach(el => {
        out.selects.push({
            id: el.id.slice(0, 80),
            optionCount: el.options ? el.options.length : 0,
            selectedIndex: el.selectedIndex,
            className: String(el.className || '').slice(0, 80)
        });
    });
    document.querySelectorAll('input[type="button"], input[type="submit"], button').forEach(el => {
        out.buttons.push({ id: el.id.slice(0, 60), value: el.value || el.textContent.slice(0, 30), cls: String(el.className).slice(0, 60) });
    });
    return out;
}"""


async def main():
    from playwright.async_api import async_playwright
    rid = sys.argv[1] if len(sys.argv) > 1 else "00197"
    reports = json.loads((BASE_DIR / "public" / "reports131.json").read_text(encoding="utf-8"))
    report = next(r for r in reports if str(r.get("hubReptNo(reptId)")).strip() == rid)
    official_id = report.get("보고서ID")
    print(f"덤프 대상: [{rid}] {report.get('보고서명')} ({official_id})")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
        context = await browser.new_context(user_agent=UA, viewport={"width": 1440, "height": 900})
        pg = await context.new_page()
        url = f"https://data.g2b.go.kr/link/AISC001_01/?reptNm={official_id}"
        await pg.goto(url, wait_until="networkidle", timeout=60000)
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

        result = {}
        for fr in popup.frames:
            try:
                info = await fr.evaluate(FRAME_DUMP_JS)
                result[fr.name or fr.url[:60]] = info
            except Exception as e:
                result[fr.name or fr.url[:60]] = {"error": str(e)[:100]}

        out_path = BASE_DIR / "data" / "columns" / f"dom_dump_{rid}.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        for fname, info in result.items():
            if "error" in info:
                print(f"◆ frame[{fname}] ERR: {info['error']}")
                continue
            print(f"\n◆ frame[{fname}] url={info['url']}")
            print(f"  inputs={len(info['inputs'])} selects={len(info['selects'])} buttons={len(info['buttons'])}")
            for i in info["inputs"][:15]:
                print(f"  - input id={i['id']!r} type={i['type']} value={i['value']!r} cls={i['className'][:50]!r}")
            for s in info["selects"][:8]:
                print(f"  - select id={s['id']!r} opts={s['optionCount']} sel={s['selectedIndex']}")
            for b in info["buttons"][:10]:
                print(f"  - btn id={b['id']!r} val={b['value']!r}")
        print(f"\n저장: {out_path}")
        await browser.close()

asyncio.run(main())
