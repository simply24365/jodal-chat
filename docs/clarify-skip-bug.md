# Clarify 반복 질문 버그 (skip 무효)

> 발견: 2026-09-19, 쿼리 "인증 제품 통계 있나". 상태: 수정됨 (아래).

## 현상

```text
U: 인증 제품 통계 있나
A: 어떤 기준으로 나눠볼까? … [소관구분별] [수요기관별] [그냥 보여줘]   ← 1차 clarify
U: (그냥 보여줘 클릭)
A: 어떤 기준으로 나눠볼까? … [소관구분별] [수요기관별] [그냥 보여줘]   ← 2차 clarify (같은 질문!)
U: (그냥 보여줘 클릭)
A: reports (best-effort)                                              ← 3턴째야 답변
```

## 원인

`decideClarify()`가 `state.skipped`를 안 봄. `__skip__::dims` 액션은
`mergeState`에서 `skipped: ["dims"]`로 기록만 하고, 다음 턴 판정에서 참조 안 함.
`clarify_n < 2` 조건만 있어서 같은 슬롯을 최대 2회까지 되묻는 구조였음.
"그냥 보여줘"의 의미(해당 슬롯 포기 → 바로 답변)가 구현에 반영 안 됨.

## 수정

- `decideClarify` 진입 시 `(state.skipped || []).includes("dims")`면 즉시 null
  → best-effort 답변으로 직행. 2차 clarify 자체가 발생하지 않음.
- 일반 규칙화: 앞으로 슬롯 종류 늘어나면 `skipped.includes(slot)` 형태로 동일 처리.
  (지금은 dims 슬롯 1종이라 하드코딩 없이 `slot` 변수로-о 일반화済.)

## 검증

- 동일 시나리오: clarify 1회 → 그냥 보여줘 → 즉시 reports (best-effort).
- 기존 e2e (107+5) 재실행 — clarify_rate 변동 확인, 회귀 없음.

## 교훈

- cap(2회)은 중복 방지책이 아님. 사용자 의사표시(skip)는 상태로 남기고 판정에 반영해야 함.
- clarify는 "묻는 행위" 자체가 비용이라, 묻지 않기로 한 슬롯은.ask 목록에서 영구 제외.
