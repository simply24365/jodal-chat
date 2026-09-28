# 분석: citation 0 + 수자원공사 오답 (2026-09-21)

쿼리: "수자원공사 올해 계약 건수 알고싶은데". 기대: 수요기관별 쪼개기 가능한 계약건수 보고서. 실제: 00610 등 수의계약 계열 + `citation 0` 10연발.

## 1. 왜 citation 0이 오는가 — 렌더 버그, 숨기는 게 맞음

**원인 (확정):** `route.ts:310-318`이 MCP 후보를 `data-citation`으로 승격할 때 `number: 0` 하드코딩. `message.tsx:43`은 `number > 0`일 때만 `[N]`을 그리고, 0이면 번호 없이 제목+이동 버튼만 그린다. 즉 "citation 0" 텍스트는 우리 코드 어느 곳에서도 찍지 않음 — 제목이 비어 보이는 ToolActivity 블록의 디버그성 fallback 표기이거나, 이전 빌드(`.next` stale) 잔재다.

**조치:** number=0 분기는 `[N]`·"citation" 라벨 없이 `보고서명 + [이동]` 버튼만 렌더하도록 `FastApiPartView` 수정. 지금처럼 "citation 0 / Completed / Result" 3줄 나열은 정보량 0이므로 통째로 접기(collapsible, 기본 접힘)가 맞음. 제목이 비어 보인다면 `title`이 빈 문자열로 온 케이스 → `report_id` fallback 표시.

## 2. 왜 엉뚱한 보고서가 나오는가 — 네 문제 아님, 검색 단계 문제

재현 (`search_reports`, w=0.7):

- slots: `{family: none, dims: [], metric: [계약건수], concepts: [contract_count]}`
- "수자원공사"는 **어느 슬롯에도 안 잡힘**. concepts 9개에 기관명 개념이 없고, family 44개는 보고서 가문명(수의계약·쇼핑몰…)이라 기관명은 매칭 대상 자체가 아님. 즉 기관 필터 신호가 파이프라인에 존재하지 않음.
- 1위 00610 (1.85): `metric 적중 +1`, `개념 적중 +0.3`, S5 RRF 가산. "계약건수" 하나만으로 뽑힌 것. 수요기관 컬럼(`수요기관명`)은 있지만 dims가 `[]`라 coverage 신호 없음.
- 기대한 "수요기관별 계약납품요구" 보고서는 카탈로그에 없음. 수요기관 컬럼 + 건수 지표 둘 다 가진 보고서는 00610(지방정부 수의계약) + 00040(지역별 지역제한) 뿐. 그것도 dims가 `[]`/`[지역]`이라 "수요기관별" 쪼개기가 명시된 보고서는 사실상 없음.

**부실한 steps (우선순위순):**

1. **개체명 슬롯 부재 (가장 큼).** "수자원공사" 같은 기관 고유명사를 잡는 슬롯·개념이 없음. `slot_parser`는 family/dims/metric/channel만 보고, `concepts.json` 9개 중 기관 관련은 `org_client`(수요기관 일반명사)뿐. 고유명사는 BM25 텍스트 유사도에만 의존 → bm25 rank 6~30, vec은 25위 밖. **fix:** 기관명 사전(공공기관 목록) + `org_name` 슬롯 추가, 컬럼값 매칭은 못해도 "수요기관 컬럼 보유 보고서 가점" 정도는 가능.
2. **S3 metric 매칭이 거침.** 쿼리 "계약 건수" → `contract_count` 개념의 label `계약건수`와 보고서 metrics `건수`가 substring 매칭(`m in x or x in m`)으로 적중. 00610의 `건수`가 수의계약 건수인데도 +1. 지표 동음이의어 해소 없음. **fix:** 단기 — metric 적중 시 "해당 지표가 주지표인지" 가중치 차등. 장기 — 지표 온톨로지.
3. **"올해" 시간 슬롯 부재.** `date` 개념은 있으나 연도 파싱 없음. 어느 보고서도 "올해 데이터 있음" 신호를 못 냄. 전부 전일(D-1) 기준이라 시간 필터는 현재 무의미 —LLM에 알려줄 필요는 있음 (`get_report_detail.desc`에 "전일 기준" 명시됨).
4. **LLM(최종 선택) 책임도 있음.** 후보 10건에 signals(`dims_coverage`, `concept_hits`, `drop_reason`)가 다 있었는데도 모델이 00610~00028을 나열만 하고 "수요기관별 쪼개기 가능 여부"를 검증 안 함. `search_reports.description`에 "수요기관 컬럼 보유 여부를 conds/dims로 확인할 것" 힌트 한 줄이면 개선 가능. 단, 후보 자체에 정답이 없으면 LLM도 못 찾음 — 이번 건은 검색 recall 문제라 1번이 본질.

## 3. 조치 목록

- [ ] `message.tsx`: number=0 citation → 버튼만, collapsible 기본접힘. "citation 0" 텍스트 제거.
- [ ] `worker.js` `mcpSignals` or description: 후보에 `has_org_col` (수요기관/발주기관 컬럼 보유) 신호 추가. LLM이 "기관별 쪼개기 가능" 판단 가능해짐.
- [ ] `slot_parser` + `concepts.json`: 공공기관명 사전 기반 `org_name` 슬롯. 없음 → BM25에만 의존하는 구조적 한계 지속.
- [ ] `search_reports.description`: "기관명이 쿼리에 있으면 has_org_col=true 후보 우선, 없으면 없다고 답할 것" 1줄.
