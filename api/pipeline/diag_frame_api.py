#!/usr/bin/env python3
"""'Frame' object has no attribute 'is_closed' 원인 규명용 진단 (신규, 기존 미수정).

가설: batch_extract_columns.py 가 Frame.is_closed() 를 호출하는데,
Playwright Frame API 에는 is_closed 가 없고 is_detached() 가 있다.
(is_closed 는 BrowserContext/Page 계열 API)
"""
from playwright.sync_api import sync_playwright

with sync_playwright() as p:
    bw = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])
    pg = bw.new_page()
    pg.goto("about:blank")
    fr = pg.main_frame
    print("has is_closed  :", hasattr(fr, "is_closed"))
    print("has is_detached:", hasattr(fr, "is_detached"))
    try:
        fr.is_closed()
        print("is_closed()  : OK (가설 기각)")
    except AttributeError as e:
        print(f"is_closed()  : AttributeError 재현 -> {e} (가설 확정)")
    try:
        print("is_detached():", fr.is_detached())
    except Exception as e:
        print("is_detached() ERR:", e)
    print("page has is_closed:", hasattr(pg, "is_closed"))
    bw.close()
