#!/usr/bin/env python3
"""LLM provider dry-run: 3사 요청/응답 확인 (키는 env, 파일에 기록 금지).

env:
  LLM7_API_KEY, LLM7_MODEL(=DeepSeek-V4-Flash-0731), LLM7_BASE(기본 https://api.llm7.io/v1)
  GROQ_API_KEY, GROQ_MODEL(=qwen/qwen3-32b)
  CEREBRAS_API_KEY, CEREBRAS_MODEL(=llama-3.3-70b)

사용: export ... && python3 tools/llm_dryrun.py
"""
import json
import os
import sys
import urllib.request

TIMEOUT = 30
TINY = [{"role": "user", "content": "ping. reply with exactly: pong"}]
# NOTE: Groq/Cerebras 앞단 Cloudflare가 Python 기본 UA를 1010 차단 → 브라우저 UA 필수
BROWSER_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


def openai_chat(base, key, model, messages):
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps({"model": model, "messages": messages,
                         "max_tokens": 20, "temperature": 0}).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": BROWSER_UA},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, json.loads(r.read().decode())


PROVIDERS = [
    ("llm7", os.environ.get("LLM7_BASE", "https://api.llm7.io/v1"),
     "LLM7_API_KEY", os.environ.get("LLM7_MODEL", "DeepSeek-V4-Flash-0731")),
    ("groq", "https://api.groq.com/openai/v1", "GROQ_API_KEY",
     os.environ.get("GROQ_MODEL", "qwen/qwen3-32b")),
    ("cerebras", "https://api.cerebras.ai/v1", "CEREBRAS_API_KEY",
     os.environ.get("CEREBRAS_MODEL", "qwen-3.8-27b")),
]


def main():
    results = {}
    for name, base, key_env, model in PROVIDERS:
        key = os.environ.get(key_env, "")
        if not key:
            print(f"[{name}] SKIP: {key_env} 미설정")
            results[name] = "SKIP_NO_KEY"
            continue
        try:
            st, body = openai_chat(base, key, model, TINY)
            try:
                txt = body["choices"][0]["message"]["content"]
            except Exception:
                txt = json.dumps(body)[:200]
            print(f"[{name}] HTTP {st} model={model} reply={txt.strip()[:100]!r}")
            results[name] = f"OK:{st}"
        except Exception as e:
            print(f"[{name}] FAIL model={model} err={str(e)[:200]}")
            results[name] = f"FAIL:{str(e)[:80]}"
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    sys.exit(main() or 0)
