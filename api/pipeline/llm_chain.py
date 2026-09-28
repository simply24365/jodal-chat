#!/usr/bin/env python3
"""LLM fallback 체인 (freetier 보호): xkiro(최우선) → agnes → groq → gemini.

gemini는 모델 단위 폴백: gemini-3.1-flash-lite → gemma-4-26b-a4b-it.
Cerebras/llm7 제외.

특성:
  - 키는 env only (파일 기록 금지): XKIRO_API_KEY, AGNES_API_KEY, GROQ_API_KEY, GEMINI_API_KEY
  - provider별 토큰버킷 rate limit (freetier 초과 방지):
      xkiro qwen/qwen3.8-max:free (https://api.xkiro.com/v1, OpenAI 호환):
                                 분당 XKIRO_RPM(기본 10), 호출간격 6초, 일 XKIRO_DAY_CAP(기본 900)
      agnes agnes-3.0-flash (https://llm.orla.cc/v1, OpenAI 호환):
                                 분당 AGNES_RPM(기본 10), 호출간격 6초, 일 AGNES_DAY_CAP(기본 900)
      groq openai/gpt-oss-120b : 분당 GROQ_RPM(기본 4, 공식 30RPM이나 8K TPM이 병목),
                                 호출간격 15초, 일 GROQ_DAY_CAP(기본 150, 200K TPD 기준)
      gemini (모델당 공유 버킷): 분당 GEMINI_RPM(기본 10), 일 GEMINI_DAY_CAP(기본 900)
  - 429 → Retry-After+버퍼 대기 후 같은 후보 1회 재시도, 그래도 429면 다음 후보.
    5xx → 10분 쿨다운(파일 persist). 400~404 → 다음 후보.
  - 일 상한 도달 시 호출 전 즉시 다음 후보 (모두 소진 시 LLMExhausted).

사용:
  from llm_chain import chat
  text = chat([{"role":"user","content":"..."}], max_tokens=512, temperature=0)
"""
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


class LLMExhausted(Exception):
    pass


COOLDOWN_PATH = Path.home() / ".cache" / "jodal_llm_cooldown.json"
# 5xx 일시 장애 시 단기 쿨다운 (429는 Retry-After로 처리, 파일 등록 안 함)
COOLDOWN_SEC = int(os.environ.get("LLM_COOLDOWN_SEC", "600"))


def _load_cooldowns():
    try:
        if COOLDOWN_PATH.exists():
            cd = json.loads(COOLDOWN_PATH.read_text(encoding="utf-8"))
            now = time.time()
            return {k: v for k, v in cd.items() if v > now}
    except Exception:
        pass
    return {}


def _save_cooldowns(cd):
    try:
        COOLDOWN_PATH.parent.mkdir(parents=True, exist_ok=True)
        COOLDOWN_PATH.write_text(json.dumps(cd, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


class Bucket:
    """간단 토큰버킷: capacity/분 + 1회 호출 간 최소 간격(초)."""

    def __init__(self, per_minute, min_interval=0.0):
        self.per_minute = per_minute
        self.min_interval = min_interval
        self.tokens = float(per_minute)
        self.updated = time.monotonic()
        self.last_call = 0.0

    def take(self):
        now = time.monotonic()
        self.tokens = min(self.per_minute,
                          self.tokens + (now - self.updated) * self.per_minute / 60.0)
        self.updated = now
        wait = max(0.0, self.min_interval - (now - self.last_call))
        if self.tokens < 1.0:
            wait = max(wait, (1.0 - self.tokens) * 60.0 / self.per_minute)
        if wait > 0:
            time.sleep(wait)
            now = time.monotonic()
            self.tokens = min(self.per_minute,
                              self.tokens + (now - self.updated) * self.per_minute / 60.0)
            self.updated = now
        self.tokens -= 1.0
        self.last_call = time.monotonic()


def _post_gemini(key, model, messages, max_tokens, temperature, timeout=90,
                 json_mode=False):
    """Gemini generateContent API (OpenAI 스키마 아님)."""
    contents = []
    for m in messages:
        role = "model" if m.get("role") == "assistant" else "user"
        contents.append({"role": role, "parts": [{"text": m.get("content", "")}]})
    payload = {"contents": contents,
               "generationConfig": {"maxOutputTokens": max_tokens,
                                    "temperature": temperature}}
    if json_mode:
        payload["generationConfig"]["response_mime_type"] = "application/json"
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        data=json.dumps(payload).encode(),
        headers={"x-goog-api-key": key, "Content-Type": "application/json",
                 "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
        parts = body["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts), None
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:300]
        except Exception:
            detail = ""
        retry_after = 0
        try:
            for d in json.loads(detail).get("error", {}).get("details", []):
                if "retryDelay" in d:
                    retry_after = int("".join(filter(str.isdigit, d["retryAfter"] or d.get("retryDelay", "0s"))) or 0)
        except Exception:
            pass
        return None, (e.code, detail, retry_after)
    except (KeyError, IndexError) as e:
        return None, (502, f"gemini 응답 파싱 실패: {e}", 0)
def _post(base, key, model, messages, max_tokens, temperature, extra=None,
          timeout=90, json_mode=False):
    payload = {"model": model, "messages": messages,
               "max_tokens": max_tokens, "temperature": temperature}
    if extra:
        payload.update(extra)
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": UA},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode())
        msg = body["choices"][0]["message"]
        # gpt-oss 계열: max_tokens가 reasoning에 먹혀 content가 비는 경우가 있음.
        # content 비어있으면 reasoning 폴백 (요약 용도이므로 허용).
        text = msg.get("content") or msg.get("reasoning") or ""
        if not text.strip():
            # 빈 content는 서버 장애가 아니라 프롬프트/토큰 문제 → 쿨다운 없이 다음 provider로
            return None, (204, "empty content", 0)
        return text, None
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:300]
        except Exception:
            detail = ""
        retry_after = 0
        try:
            retry_after = int((e.headers or {}).get("Retry-After", 0) or 0)
        except Exception:
            pass
        if not retry_after:
            try:
                retry_after = int(json.loads(detail).get("error", {}).get("retry_after", 0) or 0)
            except Exception:
                pass
        return None, (e.code, detail, retry_after)


# 모델 순서 (사용자 지정): xkiro → agnes → groq → gemini 3.1-flash-lite → gemma-4-26b-a4b-it
GEMINI_MODELS = ["gemini-3.1-flash-lite", "gemma-4-26b-a4b-it"]
XKIRO_BASE = "https://api.xkiro.com/v1"
AGNES_BASE = "https://llm.orla.cc/v1"


def _providers():
    provs = []
    if os.environ.get("XKIRO_API_KEY"):
        provs.append({
            "name": "xkiro",
            "api": "openai",
            "base": os.environ.get("XKIRO_BASE_URL", XKIRO_BASE),
            "key": os.environ["XKIRO_API_KEY"],
            "model": os.environ.get("XKIRO_MODEL", "qwen/qwen3.8-max:free"),
            # rate-limit 헤더 미노출이라 agnes와 같은 보수적 기본값 (env로 상향 가능)
            "bucket": Bucket(per_minute=int(os.environ.get("XKIRO_RPM", "10")),
                             min_interval=float(os.environ.get("XKIRO_MIN_INTERVAL", "6"))),
            "day_cap": int(os.environ.get("XKIRO_DAY_CAP", "900")),
            "used_today": 0,
        })
    if os.environ.get("AGNES_API_KEY"):
        provs.append({
            "name": "agnes",
            "api": "openai",
            "base": os.environ.get("AGNES_BASE_URL", AGNES_BASE),
            "key": os.environ["AGNES_API_KEY"],
            "model": os.environ.get("AGNES_MODEL", "agnes-3.0-flash"),
            "bucket": Bucket(per_minute=int(os.environ.get("AGNES_RPM", "10")),
                             min_interval=float(os.environ.get("AGNES_MIN_INTERVAL", "6"))),
            "day_cap": int(os.environ.get("AGNES_DAY_CAP", "900")),
            "used_today": 0,
        })
    if os.environ.get("GROQ_API_KEY"):
        provs.append({
            "name": "groq",
            "api": "openai",
            "base": "https://api.groq.com/openai/v1",
            "key": os.environ["GROQ_API_KEY"],
            "model": os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
            # 공식 30RPM이나 호출당 수천 토큰(W2c 기준)이라 8K TPM이 병목 → 4RPM+15초 간격
            "bucket": Bucket(per_minute=int(os.environ.get("GROQ_RPM", "4")),
                             min_interval=float(os.environ.get("GROQ_MIN_INTERVAL", "15"))),
            "day_cap": int(os.environ.get("GROQ_DAY_CAP", "150")),
            "used_today": 0,
        })
    if os.environ.get("GEMINI_API_KEY"):
        bucket = Bucket(per_minute=int(os.environ.get("GEMINI_RPM", "10")),
                        min_interval=6.0)  # freetier 여유 (분당 10회)
        cap = int(os.environ.get("GEMINI_DAY_CAP", "900"))
        models = ([os.environ["GEMINI_MODEL"]] if os.environ.get("GEMINI_MODEL")
                  else list(GEMINI_MODELS))
        for m in models:
            provs.append({
                "name": f"gemini:{m}",
                "api": "gemini",
                "key": os.environ["GEMINI_API_KEY"],
                "model": m,
                "bucket": bucket,  # 모델끼리 버킷 공유 (키 단위 quota)
                "day_cap": cap,
                "used_today": 0,
            })
    return provs


def chat(messages, max_tokens=512, temperature=0, timeout=90, verbose=True,
         json_mode=False):
    """순서대로 시도, 성공 시 텍스트 반환. 전부 실패 시 LLMExhausted."""
    log = (lambda s: print(s, flush=True)) if verbose else (lambda s: None)
    last_err = "no providers"
    cooldowns = _load_cooldowns()
    for p in _providers():
        cd_until = cooldowns.get(p["name"], 0)
        if cd_until > time.time():
            remain = int((cd_until - time.time()) // 60)
            log(f"[{p['name']}] SKIP: 쿨다운 중 (약 {remain}분 남음)")
            continue
        if p["used_today"] >= p["day_cap"]:
            log(f"[{p['name']}] SKIP: 일 상한({p['day_cap']}) 도달")
            continue
        p["bucket"].take()
        log(f"[{p['name']}] 요청 ({p['used_today']+1}/{p['day_cap']})")
        if p.get("api") == "gemini":
            text, err = _post_gemini(p["key"], p["model"], messages,
                                     max_tokens, temperature, timeout=timeout,
                                     json_mode=json_mode)
        else:
            text, err = _post(p["base"], p["key"], p["model"], messages,
                              max_tokens, temperature, timeout=timeout,
                              json_mode=json_mode)
        if text is not None:
            p["used_today"] += 1
            log(f"[{p['name']}] OK ({len(text)}자)")
            return text
        code, detail, retry_after = err
        last_err = f"{p['name']}:{code} {detail[:120]}"
        log(f"[{p['name']}] FAIL {code} {detail[:150]}")
        if code == 204:
            # 빈 content → 쿨다운 없이 다음 provider로 즉시 폴백
            continue
        if code == 429:
            # 429는 파일 쿨다운 등록 안 함: Retry-After+버퍼 대기 후 같은 후보 1회 재시도
            wait = min((retry_after or 15) + 5, 120)
            log(f"[{p['name']}] 429 → {wait}s 대기 후 같은 후보 1회 재시도")
            time.sleep(wait)
            p["bucket"].take()
            if p.get("api") == "gemini":
                text2, err2 = _post_gemini(p["key"], p["model"], messages,
                                           max_tokens, temperature,
                                           timeout=timeout, json_mode=json_mode)
            else:
                text2, err2 = _post(p["base"], p["key"], p["model"], messages,
                                    max_tokens, temperature, timeout=timeout,
                                    json_mode=json_mode)
            if text2 is not None:
                p["used_today"] += 1
                log(f"[{p['name']}] OK ({len(text2)}자, 재시도 성공)")
                return text2
            code2, detail2, _ = err2
            last_err = f"{p['name']}:{code2} {detail2[:120]}"
            log(f"[{p['name']}] 재시도 FAIL {code2} {detail2[:150]} → 다음 후보")
            if code2 in (500, 502, 503, 504):
                cooldowns[p["name"]] = time.time() + COOLDOWN_SEC
                _save_cooldowns(cooldowns)
                log(f"[{p['name']}] 쿨다운 {COOLDOWN_SEC//60}분 등록")
            continue
        if code in (500, 502, 503, 504):
            # 5xx 일시 장애 → 단기 쿨다운 (파일 persist, 다음 실행에도 적용)
            cooldowns[p["name"]] = time.time() + COOLDOWN_SEC
            _save_cooldowns(cooldowns)
            log(f"[{p['name']}] 쿨다운 {COOLDOWN_SEC//60}분 등록")
            continue
        if code in (400, 401, 402, 403, 404):
            log(f"[{p['name']}] 인증/잔액/모델 문제 → 다음 후보")
            continue
        # 5xx/기타 → 다음 provider로 폴백
    raise LLMExhausted(f"전 provider 실패: {last_err}")


def main():
    text = chat([{"role": "user", "content": "reply with exactly: pong-chain"}],
                max_tokens=20)
    print("CHAIN_REPLY:", text.strip()[:100])


if __name__ == "__main__":
    try:
        main()
    except LLMExhausted as e:
        print("EXHAUSTED:", e)
        sys.exit(1)
