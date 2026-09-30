# 조달데이터허브 임베드 — iframe 계약 (결론: iframe, 스크립트 번들 아님)

2026-09-30. 목적: `https://data.g2b.go.kr` (WebSquare + Spring 전자정부프레임워크) 페이지에
`jodal.orla.cc` 채팅을 넣되, **조달데이터허브 운영 코드 수정 0건**으로 붙이는 방법.

결론부터: **iframe 으로 간다.** 조달 허브 쪽 변경은 정적 HTML 파일 1개다.

## 왜 iframe 이 정답인가

### 1. 지금 바로 붙는다 — 프레임 차단 헤더가 없다

```console
$ curl -sI https://jodal.orla.cc/
HTTP/2 200
content-type: text/html; charset=utf-8
cf-cache-status: DYNAMIC
server: cloudflare
```

`X-Frame-Options` 없음, `Content-Security-Policy: frame-ancestors` 없음. 따라서 지금
코드를 건드리지 않아도 타 도메인에서 iframe 임베드가 통과한다.

주의: "헤더가 없어서 된다"는 건 허용이 아니라 **방어선이 없다는 뜻**이다. 나중에
`next.config.ts` 에 헤더가 들어가는 사람이 있으면 조용히 깨진다. §5 의 명시 추가를 한다.

### 2. 이 앱은 이미 세션·DB 를 쓰지 않는다 — 서드파티 쿠키 문제가 구조적으로 없음

externals 임베드에서 가장 자주 막히는 사유는 "외부 도메인이 인증을 요구한다"다.
이 포크는 그 부분이 이미 무력화돼 있다.

```ts
// ui/proxy.ts:3
// Auth neutralized: FastAPI owns no users. All routes pass through.

// ui/app/(chat)/api/history/route.ts:3
// Auth/DB neutralized: history lives in FastAPI sessions + browser state.

// ui/app/(chat)/api/messages/route.ts:1
// Auth/DB neutralized: FastAPI sessions own history. New chats start empty.
```

`ui/proxy.ts` 의 매처가 전 라우트를 통과시키고, 히스토리는 FastAPI 세션 + 브라우저 상태가
갖는다. **세션 쿠키도 조달청 DB 접근도 없다.** 그래서 서드파티 쿠키를 기본 차단하는
브라우저(Safari) 에서도 로그인 파손이 없고, iframe 안에 조달청의 인증 자원을 끌고 오지도
않는다. 임베드 심의에서 이 사유로 거절당할 확률을 크게 낮춘다.

### 3. FastAPI 에 CORS 가 없다 — 스크립트 직접 호출은 지금 불가

`api/app/main.py` 에 `CORSMiddleware` 가 없다. 조달청 페이지(origin)에서 `fetch` 로
직접 부르면 브라우저가 막는다. iframe 은 CORS 와 무관하므로 이 문제가 아예 발생하지 않는다.

### 4. Next 앱을 "HTML 블록 + script" 하나로 만들 수 없다

`next build` 산출물은 독립 실행이 안 된다. `app/(chat)/api/*` 라우트가 서버에서 돌아가고
RSC/SSR 이 얽혀 있어, 정적 파일에 script 태그 하나 박아서는 채팅이 동작하지 않는다.

단일 JS 파일을 원한다면 client-only 번들을 esbuild/vite 로 **두 번째 빌드 경선**으로 만들어야
하는데, 비용이 iframe 보다 크다.

| | iframe | 단일 script 번들 |
|---|---|---|
| 빌드 경선 | 추가 없음 | 별도 번들 빌드 + CI 붙임 |
| CORS | 무관 | FastAPI 에 CORS 추가 필요 |
| CSS 격리 | 프로세스 경계로 보장 | WebSquare CSS 와 충돌 가능 |
| 부모→자 상태 전달 | `postMessage` | DOM 직접 접근 |
| 실패 시 영향 | 빈 프레임 | 부모 페이지 스크립트 오염 가능 |

## 조달데이터허브 쪽 변경 — 정적 파일 1개

Spring 컨트롤러·서비스·DAO 를 건드리지 않는다.

```html
<!-- resources/static/jodal.html -->
<!doctype html>
<html lang="ko">
  <head><meta charset="utf-8" /><title>조달데이터허브 AI</title></head>
  <body style="margin:0">
    <iframe
      id="jodal-frame"
      src="https://jodal.orla.cc/embed?src=hub"
      title="조달데이터허브 AI"
      style="width:100%;height:640px;border:0"
      loading="lazy"
    ></iframe>
    <script>
      // 크로스오리진이라 부모가 iframe 높이를 못 읽는다. 자식이 postMessage 로
      // 보고하는 내용 높이로 맞춘다 (§4).
      const f = document.getElementById("jodal-frame");
      window.addEventListener("message", (e) => {
        if (e.origin !== "https://jodal.orla.cc") return;
        if (e.data?.type !== "jodal:height") return;
        const h = Math.max(320, Math.min(Number(e.data.height) || 640, 1600));
        f.style.height = h + "px";
      });
    </script>
  </body>
</html>
```

- **Spring MVC**: 정적 리소스로 서빙된다. 컨트롤러가 필요하다면 리다이렉트 1줄뿐 —
  `@GetMapping("/chat")` → `return "forward:/jodal.html";`
- **WebSquare**: XML 엔진이라 HTML5 태그는 `html5:` 접두어로 표기한다
  (`<html5:iframe>`). 접두어 표기는 사내 프레임워크 문서에서 확인할 것. 애매하면
  위 정적 HTML 을 include 하는 쪽이 안전하다.
- **JSP**: `<iframe>` 태그 하나면 끝. 스크립트let 조각이면 위 `<script>` 만 같이 넣는다.

## 이 저장소(jodal-chat) 쪽에서 만들 것

`jodal.orla.cc` 는 자유롭게 고칠 수 있으므로 임베드 전용 화면을 이쪽에 만든다.
조달 허브가 아니라 **여기**를 건드린다.

### 1. `ui/app/embed/page.tsx` — 최소 채팅 화면

사이드바·모델 선택 툴바·투표 버튼·아티팩트 패널을 전부 뺀 chat 위젯. 기존
`ChatShell`(`ui/components/chat/shell.tsx`) 과 `ActiveChatProvider`
(`ui/hooks/use-active-chat.tsx`) 를 재사용하면 chat id 생성·전송·스트림 처리는 그대로
물려받는다. `ActiveChatProvider` 는 URL 에서 `/chat/<id>` 를 뽑는데 `/embed` 은 매칭되지
않으므로 내부 UUID 로 폴백한다 — 그대로 쓴다.

### 2. 높이 자동 보고 (postMessage)

크로스오리진이라 부모가 `iframe.contentDocument` 를 못 읽는다. `/embed` 가 메시지 루트 높이를
`postMessage` 하고, §3 스니펫의 리스너가 `iframe.style.height` 에 반영한다. 이 계약이 없으면
높이 640px 에 잘린다.

```ts
// ui/app/embed/page.tsx 안 — 컨텐츠가 늘어날 때마다
window.parent.postMessage({ type: "jodal:height", height: h }, "https://data.g2b.go.kr");
```

`targetOrigin` 은 하드코딩하지 말고 배포 대상을 설정으로 받는다(조달청 외 도메인에도 넣을
여지). 미정 상태로 두면 `*` 로 쏘게 되는데, 그건 되도록 피한다.

### 3. `frame-ancestors` 명시

`ui/next.config.ts` 의 `headers()` 에 아래를 넣는다. 허용 목록이 좁아지므로 실제로 임베드할
도메인(및 개발용 `localhost`)을 명시한다.

```
Content-Security-Policy: frame-ancestors 'self' https://data.g2b.go.kr
```

헤더가 없을 때의 "허용"은 실수가 unnoticed 하게 남는 구조다. 의도를 코드로 고정한다.

### 4. 장애 격리

이 URL 이 죽어도 조달 허브는 빈 프레임만 본다. 운영 코드에 영향이 없어야 한다는 조건의
본질적인 절반은 "장애가 전파되지 않는다"다. 부모 페이지에 폴백 문구를 둔다.

```html
<noscript>AI 조회 기능이 필요 없습니다.</noscript>
```

## 리스크

| # | 리스크 | 현재 상태 | 대처 |
|---|---|---|---|
| 1 | 외부 도메인 임베드가 전자정부프레임워크 보안정책 심의 대상 | 미확인 | 사내 심의 필요. 기술적으로 iframe 이 가장 유리 (§2) |
| 2 | 이 박스는 4코어 / 22G, Onyx 10컨테이너와 동시 가동 | `free -h` = 22Gi, used 11Gi | 트래픽 예측 후 용량 확인. 조달청 페이지에 박히면 이 박스로 온다 |
| 3 | 조달청 도메인에 조달청 DB 로 만든 툴이 그대로 노출 | `local_meta.json` 에 hubpick ver `w2d-20260919` 표시 | 심의 전 툴 결과에 실제 내부 값이 담기는지 별도 검토 |
| 4 | `jodal.orla.cc` 는 공인 인터넷 주소 (OCI Always Free + cloudflared 터널) | `/etc/cloudflared/config.yml` | 사내용 배포가 필요하면 별도 건. 경로는 §6 참고 |

## 데이터 마이그레이션 시 영향 범위

나중에 실제 조달청 DB 를 크롤링해 assets·vectors 를 갈아끼워도 **iframe 계약은 바뀌지 않는다**.
`api/data/index/` 와 `api/data/hubpick/` 안의 산출물만 교체된다.

- 조달 허브 수정: 계속 **0건**
- `api/` 내부: 인덱스 빌드 파이프라인 교체 (`pipeline/build_hybrid_index.py` 대상)
- `ui/`: 없음

## 부록 — 배포 형태 (현행)

`jodal.orla.cc` 는 Vercel 이 아니다. Oracle Linux(`myalwaysfreearm`, OCI Always Free ARM)
에서 프로세스 2개(127.0.0.1) + cloudflared 터널로만 외부 노출된다.

```bash
api/  FastAPI                      127.0.0.1:8078   (agent loop + MCP /mcp)
ui/   Next.js 16                   127.0.0.1:3002   (chat UI)
cloudflared tunnel                 jodal.orla.cc → 127.0.0.1:3002
```

기동·점검:

```bash
make start / make stop     # nohup + PID 파일 (systemd 유닛 없음)
make health                # /health /tools
make mcp-smoke             # 툴 11개 목록 + 대표 호출 3건
```

주의: **systemd 유닛이 없다.** `README.md` 의 "systemd 프로세스 2개" 서술과 실제 상태가
다르다. 재부팅하면 자동으로 올라오지 않는다. 임베드로 공개 전에 유닛화할 것을 권한다.