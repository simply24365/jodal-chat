# ui/ — upstream 출처

이 디렉터리는 [`vercel/ai-chatbot`](https://github.com/vercel/ai-chatbot) 의 복제본이다.
`jodal-chat` 에서 원본 upstream 저장소를 그대로 clone 해 왔고, 그 위에 로컬 변경을 얹었다.

## upstream 기준점

| 항목 | 값 |
|---|---|
| upstream | `https://github.com/vercel/ai-chatbot` |
| base 커밋 | `c2f8235` ("Check Bot ID result (#1529)") |
| 로컬 커밋 | `af60101` — FastAPI adapter: `/chat` NDJSON → UIMessage + move buttons<br>`f609601` — 멀티턴 맥락 전달 복구 + citation 제목에서 `report_id` 제거 |

`ui/.git` 은 제거했다(저장소 구조 단순화). 그래서 upstream base 를 추적하려면
**이 문서의 base 커밋 정보가 유일한 기준**이다. upstream 을 따라가려면:

```bash
# 1. upstream base 를 임시로 복원
git init ui && git -C ui remote add upstream https://github.com/vercel/ai-chatbot
git -C ui fetch upstream

# 2. base 이후 로컬 커밋만 위에서 다시 쌓기
git -C ui checkout -B jodal upstream/c2f8235
#   (이후 로컬 변경을 cherry-pick / 재적용)
```

## 로컬에서 필요한 설정

`ui/.env.local` 은 gitignore 대상이라 클론에 없다. 만들어야 한다:

```bash
cat > ui/.env.local <<'EOF'
AUTH_SECRET=<openssl rand -base64 32>
CUSTOM_CHAT_BACKEND_URL=http://127.0.0.1:8078
EOF
```

`CUSTOM_CHAT_BACKEND_URL` 이 `api/` 를 가리킨다. 이 값이 없으면 챗이 조달
정보를 못 받는다 — UI 가 이 주소를 통해서만 `api/` 와 통신한다.

## 구조 변경 이력

2026-09-28 저장소가 `chatbot/` → `ui/` 로 이동했다(2단 구조: `api/` + `ui/`).
`make ui` 또는 `cd ui && pnpm start --port 3002` 로 뜬다.
