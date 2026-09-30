# 조달데이터허브 chat 모달 적용 — 운영 무영향 배치 계획

2026-09-30. 대상: `https://data.g2b.go.kr/` (WebSquare + Spring 전자정부프레임워크, **Kong 게이트웨이 뒤**).
목표: 이 채팅을 모달(또는 채팅 UI)로 그대로 얹되, **운영 코드 수정 0건**으로.

앞 문서(`docs/embed-in-hub.md`)는 "iframe 이 정답"이라는 결론이었다. 이 문서는 그 결론을
**실제로 먹히는 코드로 내리는 실행 계획**이다. 오늘 이 박스에서 검증한 것만 쓴다.

## 결론

**`jodal.orla.cc` 를 iframe 으로 그대로 심는다. `src="https://jodal.orla.cc/"`.**

별도 화면도, 새 빌드도, 계정도 필요 없다. 오늘 실제로 이렇게 붙여서 대화까지 끝냈다.
최소 `/embed` 화면은 **선택 사항이지 전제 조건이 아니다** — 지금 있는 화면을 그대로 쓰면
오늘 하루 만에 심을 수 있다.

## 오늘 검증한 것 (실측)

### 1. 크로스오리진 iframe 으로 실제 대화가 된다

`docs/embed-modal-test.html` 은 운영 페이지 흉내 + 모달 + iframe 로 만든 검증 하네스다.
http://127.0.0.1:8099 (origin 이 jodal.orla.cc 와 다름)에서 열고:

```
frames: ["http://127.0.0.1:8099/embed-modal-test.html", "https://jodal.orla.cc/"]
iframe innerWidth 880 → docWidth 880 (가로 넘침 없음, 사이드바는 48px 레일로 축소)
질의 "소관구분별 녹색제품 실적통계 보고서 있어?" → 20초에 답변 + [1][101] 인용
+ "바로열기: 소관구분별 녹색제품 실적통계" 링크 + 툴 카드 2건
```

스크린샷으로 렌더링까지 확인했다. **세션 쿠키·로그인 없이 동작한다.**

### 2. 심을 대상(jodal.orla.cc)은 iframe 을 막지 않는다

```console
$ curl -sI https://jodal.orla.cc/
HTTP/2 200
x-frame-options            (없음)
content-security-policy    (없음)
```

### 3. 심는 곳(data.g2b.go.kr)은 SSO 뒤에 있고 CSP 가 없다

```console
$ curl -sI https://data.g2b.go.kr/
HTTP/1.1 302 Found
location: https://sso.g2b.go.kr/oidc/597b7826dd57ff1f/auth?...&client_id=P010040&...
x-frame-options: SAMEORIGIN
x-kong-upstream-latency: 22
set-cookie: JSESSIONID=...; Secure; HttpOnly; SameSite=Lax
```

- **전 포털이 OIDC SSO 뒤에 있다.** 사용자는 로그인된 상태로 본다.
- **`x-frame-options: SAMEORIGIN` 은 우리와 무관하다.** 그건 data.g2b.go.kr 을 다른 site 가
  frame 으로 못 넣게 하는 값이지, 우리가 그 안에 iframe 을 넣는 걸 막지 않는다.
- **`content-security-policy` 헤더가 없다.** `frame-src` 제한이 없다.
- `SameSite=Lax` 인 `JSESSIONID` 는 cross-site iframe 에서 안 넘어오지만, **우리가 필요로
  하지 않는다**(§1 참조).

### 4. 이 앱은 auth·DB 를 쓰지 않는다 — 심을 때 짐이 되지 않는다

```ts
// ui/proxy.ts:3                Auth neutralized: FastAPI owns no users. All routes pass through.
// ui/app/(chat)/api/history/route.ts:3   Auth/DB neutralized: history lives in FastAPI sessions + browser state.
```

첫 init 커밋(`1bdebb9`)에서 지금까지 손대지 않은 코드다. 그래서 iframe 안에 조달청의 인증
자원이나 세션이 끌려 들어가지 않는다. 외부 임베드에서 가장 자주 거절당하는 사유("외부
도메인이 사용자 인증을 요구한다")가 원천 없음.

## 배치 — 조달데이터허브 쪽에서 할 일

### 파일 1개, 수정 0건

`src/main/resources/static/` 아래에 정적 HTML 을 하나 던진다. 컨트롤러·서비스·DAO 를
건드리지 않는다. 리다이렉트가 필요해도 1줄뿐이다.

```java
@GetMapping("/chat")
public String chat() { return "forward:/jodal-chat.html"; }   // 선택
```

아래는 검증에 쓴 `docs/embed-modal-test.html` 그대로다. 값만 바꾸면 바로 넣는다.

```html
<!-- resources/static/jodal-chat.html -->
<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8" />
<title>조달데이터허브 AI</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; padding: 24px; background: #f6f7f9; }
  .page { background: #fff; border: 1px solid #d9dde3; border-radius: 8px; padding: 16px; }
  #open { padding: 8px 16px; font: inherit; cursor: pointer; }
  /* WebSquare 자체 레이어보다 위. z-index 충돌 여부는 배포 전 화면에서 반드시 본다. */
  .ov { position: fixed; inset: 0; background: rgba(15,20,28,.45); display: none; z-index: 2147483000; }
  .ov.on { display: flex; align-items: center; justify-content: center; }
  .dlg { background: #fff; border-radius: 10px; width: min(880px, 92vw); height: min(680px, 88vh);
         display: flex; flex-direction: column; overflow: hidden; }
  .hd { display: flex; justify-content: space-between; align-items: center; padding: 10px 14px; border-bottom: 1px solid #e5e8ec; }
  #frame { flex: 1; border: 0; width: 100%; }
</style>
</head>
<body>
<div class="page">
  <h2>조달데이터허브</h2>
  <button id="open">AI 조회 열기</button>
</div>

<div class="ov" id="ov">
  <div class="dlg">
    <div class="hd"><strong>조달데이터허브 AI</strong><button id="close">닫기</button></div>
    <iframe id="frame" src="https://jodal.orla.cc/" title="조달데이터허브 AI" loading="lazy"></iframe>
  </div>
</div>

<script>
  const ov = document.getElementById("ov");
  const frame = document.getElementById("frame");
  document.getElementById("open").onclick = () => { ov.classList.add("on"); };
  document.getElementById("close").onclick = () => { ov.classList.remove("on"); };
  ov.onclick = (e) => { if (e.target === ov) ov.classList.remove("on"); };
  // 크로스오리진이라 부모가 iframe 높이를 못 읽는다. /embed 를 만들면 자식이
  // {type:"jodal:height", height} 를 postMessage 로 보내고 여기서 반영한다.
  window.addEventListener("message", (e) => {
    if (e.origin !== "https://jodal.orla.cc") return;
    if (e.data?.type !== "jodal:height") return;
    const h = Math.max(320, Math.min(Number(e.data.height) || 680, 1600));
    frame.style.height = h + "px";
  });
</script>
</body>
</html>
```

`postMessage` 리스너는 지금은 아무 일도 하지 않는다. `/embed` 페이지를 만들게 되면 그때
살아나고, 그전까지는 무해하다.

### 이 코드가 WebSquare 에 의존하지 않는다

모달·오버레이·iframe 전부 평범한 HTML/CSS/JS 다. WebSquare API 를 한 번도 쓰지 않는다.
그래서:

- WebSquare 페이지(XML)에 넣을 때: 정적 HTML 을 iframe/include 로 부르거나,
  아니면 페이지 안의 버튼에서 `<html5:iframe>` 으로 이 정적 파일을 부른다.
- **WebSquare 태그 접두어(`html5:`) 표기는 사내 문서에서 확인할 것** — 나는 이 프레임워크의
  문서를 갖고 있지 않다. 위 코드가 그 접두어를 쓰지 않으므로, 이 부분만 미확인이다.

## 운영 코드 영향 = 0

| 대상 | 변경 |
|---|---|
| 컨트롤러 | 없음 (리다이렉트가 필요하면 `forward:` 1줄) |
| 서비스 / DAO / 배치 / 스케줄러 | 없음 |
| DB 스키마 | 없음 |
| 기존 화면 마크업 | 없음 (신규 정적 파일 1개 추가) |
| 배포 방식 | 정적 리소스 1개 추가 후 기존 절차 그대로 |
| 롤백 | 그 파일 삭제. war 재배치 한 번 |

즉 **"AI 조회 열기" 버튼을 뗐다가 다시 얹는 것**과 위험도가 같다.

## 검증 체크리스트 (배포 전, 이 순서)

`docs/embed-modal-test.html` 을 로컬에서 띄우고 아래를 눈으로 확인한다. 오늘 1~5 는 통과했다.

1. [x] 다른 origin 의 페이지에서 `https://jodal.orla.cc/` iframe 이 렌더된다
2. [x] iframe 안에서 채팅이 되고 답이 스트리밍된다
3. [x] 인용 `[N]` 과 "바로열기" 링크가 뜬다
4. [x] 880px 폭에서 가로 스크롤이 생기지 않는다
5. [x] 세션 쿠키 없이 동작한다 (로그인 프롬프트 없음)
6. [ ] **로그인 후 실제 g2b 페이지에서** CSP `frame-src` 가 외부 도메인을 허용하는지 확인
       (루트 응답에는 CSP 헤더가 없었지만, 로그인 후 화면은 확인 못 했다)
7. [ ] WebSquare 의 own 레이어(z-index)가 오버레이를 덮지 않는지
8. [ ] 모바일 폭(375px)에서 레이아웃이 무너지지 않는지

## 리스크

| # | 리스크 | 상태 |
|---|---|---|
| 1 | 외부 도메인 iframe 이 전자정부프레임워크 보안정책 심의 대상 | **미확인 — 네 쪽 관문.** 기술적으로는 §4 때문에 가장 유리한 형태다 |
| 2 | jodal.orla.cc 는 OCI Always Free 박스(4코어/22G, Onyx 10컨테이너와 동시 가동) | 확인됨. 조달청 페이지에 박히면 트래픽이 이 박스로 온다 |
| 3 | 이 박스가 죽으면 빈 모달만 남는다 |设计上 허용. 운영 페이지 기능 저하는 없음 |
| 4 | 툴 결과에 조달청 내부 값이 그대로 노출된다 | 검토 필요 (`hubpick_ver w2d-20260919`) |
| 5 | Chat UI 의 사��바·투표 버튼이 모달엔 불필요하다 | 미해결. §선택 사항 참조 |

## 선택 사항 — `/embed` 최소 화면

지금 붙이는 건 사이드바·툴바·투표 버튼이 붙은 **전체 화면**이다. 모달에는 과하다.

`/embed` 를 만들면:

- 사이드바·툴바·투표 제거 → 높이 저됨 → `postMessage` 높이 계약이 실제로 쓰인다
- `ui/components/chat/shell.tsx`(`ChatShell`) + `ActiveChatProvider` 재사용이라
  전송·스트림 처리 코드는 그대로 물려받는다
- 빌드 후 iframe `src` 를 `/embed` 로 바꾸면 끝 (조달 허브 수정은 여전히 0)

**하지 않는다:** 이건 모달 붙이는 데 전제 조건이 아니다. 붙이고 나서 판단해도 된다.
먼저 붙였다가 사이드바가 거슬리면 그때 `/embed` 를 만든다 — 그게 변경 최소화 순서다.

## 하지 말아야 할 것

- **프록시로 조달청 서버를 경유시키지 않는다.** Spring 컨트롤러를 추가해 `/chat` 을 중계하면
  운영 코드에 계속 상태(장애·타임아웃·세션)가 남는다. iframe 은 이 박스 장애가 그대로
  격리된다.
- **FastAPI 에 CORS 를 열어주지 않는다.** iframe 경로에는 필요 없고, 열어두면
  나중에 "허용 도메인" 관리가 생긴다. (`api/app/main.py` 에 `CORSMiddleware` 없음 — 정상)
- **조달청 DB 를 이 박스에서 직접 조회하지 않는다.** 인덱스는 `api/data/` 산출물로만 쓴다.
  나중에 실제 크롤링으로 교체하더라도 iframe 계약은 그대로다.