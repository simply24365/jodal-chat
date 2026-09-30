# data.g2b.go.kr 접근 조사 기록

2026-09-30. 조달데이터허브에 chat 을 붙이는 계획(`docs/embed-modal.md`)을 세우면서
확인한 사실. **인증 우회는 하지 않았다.** 공개 응답으로 확인 가능한 것만 적는다.

## 결론부터

| 하고 싶은 것 | 가능? | 근거 |
|---|---|---|
| 실제 포털 HTML/CSS/JS 받기 | **불가** | 포털이 SSO 뒤. 인증 없는 요청은 전부 404 셸 |
| 실제 화면 스크린샷 | **불가** | 같은 이유 |
| 인증 없이 공개되는 자산 확인 | 가능 | 오류 페이지 자산은 200 응답 |

## 확인한 응답

```console
$ curl -sI https://data.g2b.go.kr/
HTTP/1.1 302 Found
location: https://sso.g2b.go.kr/oidc/597b7826dd57ff1f/auth?...&client_id=P010040&...
x-frame-options: SAMEORIGIN
x-kong-upstream-latency: 22
set-cookie: JSESSIONID=...; Secure; HttpOnly; SameSite=Lax
set-cookie: XTVID=...; domain=g2b.go.kr
x-content-type-options: nosniff
strict-transport-security: max-age=31536000 ; includeSubDomains
access-control-allow-origin: *
```

**포털 전체가 OIDC SSO 뒤에 있다.** Kong 게이트웨이 앞단에 서 있다. 사용자는
로그인된 상태로 본다.

`x-frame-options: SAMEORIGIN` 은 **data.g2b.go.kr 을 다른 site 가 frame 으로 못 넣게
하는 값**이다. 우리가 그 안에 iframe 을 넣는 것을 막지 않는다. 또 `content-security-policy`
헤더가 없어 `frame-src` 제한도 없다.

## catch-all 404 셸

경로와 무관하게 없는 경로면 아래 셸(1,030바이트)이 200 으로 돌아온다.

```console
$ curl -s -o /dev/null -w "%{http_code} %{size_download}\n" https://data.g2b.go.kr/css/style.css
200 1030

$ curl -s https://data.g2b.go.kr/css/style.css | head -6
<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <title>시스템 접근 안내</title>
    ...
    <p class="con_tit">요청하신 페이지를 찾을수 없습니다.</p>
```

`<title>시스템 접근 안내</title>` 이 안 나오면 실제 콘텐츠가 아니라 이 셸이다.
프로bing 할 때判별 기준으로 쓴다.

## robots.txt

```
User-agent: Googlebot
Disallow: /fm/fma/fmaa/Pst/*.do
```

`/static/info/` 하위는 제한 대상이 아니다. 공통 자산 경로는 `/static/info/` 아래다.

## 배포 전 남은 확인 항목

목록:

- [ ] **로그인 후 실제 화면의 CSP** — 루트엔 CSP 헤더가 없었지만 인증 후 화면은
      확인하지 못했다. `frame-src` 가 외부 도메인을 허용하는지.
- [ ] **WebSquare 태그 접두어** — XML 엔진이라 HTML5 태그는 `html5:` 접두어로 표기한다고
      가정했다. 사내 프레임워크 문서에서 확인 필요.
- [ ] **z-index 충돌** — WebSquare 자체 레이어가 오버레이를 덮지 않는지
- [ ] **모바일 폭(375px)** 레이아웃

브랜드 자산(나라장터 CI·파비콘)은 대외 공개 전 사용약관 확인이 선행되어야 한다.
색·폰트 수치만으로도 시안은 설명되므로, 자산을 내려받아 스타일링하는 방법은 쓰지 않는다.