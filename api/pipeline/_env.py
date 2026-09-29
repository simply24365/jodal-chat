"""pipeline 공용: api/.env 자체 로딩 (표준 라이브러리만, 크로스플랫폼).

Windows(uv run)와 Oracle Linux(bash sourcing / make) 어디서든 pipeline 스크립트가
동일하게 키를 보도록 한다. 우선순위: 이미 설정된 환경변수(셸) > .env.
BOM·CRLF·export 접두어·따옴표 주석 허용.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def load_env(path: Path) -> None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        else:
            val = val.split(" #", 1)[0].strip()
        if val:
            os.environ.setdefault(key, val)


API_DIR = Path(__file__).resolve().parent.parent
load_env(API_DIR / ".env")
