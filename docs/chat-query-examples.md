# 조달챗 질의 예시 30 + 한계 5

기준: live 11툴 (`search_reports`, `get_report_detail`, `resolve_items`, `item_children`,
`item_detail`, `item_products`, `form_guide`, `concept_reports`, `value_lookup`,
`family_map`, `catalog_health`). 전부 E2E 실측 기반. `PASS`는 backend `:8078` 또는
live `/mcp`에서 확인된 패턴.

## 효과 질의 30

### 보고서 찾기 (search_reports + get_report_detail)

1. "수요기관별 계약납품요구 실적통계 보고서 찾아줘"
   - 도구: `search_reports` → `get_report_detail(report_id=00262)`
   - 기대: 00262 + UI-ADOXFA-105R + 바로열기 링크. PASS 실측.
2. "한전 조달 실적 보려면 뭔 보고서"
   - 도구: `search_reports(한전 조달 실적)` → `get_report_detail` 1~2건
   - 기대: 00262 계열 + 수요기관=한국전력공사 필터 안내. 한전 단독 보고서는 없다고 명시.
3. "입찰공고 내역 보고서 상세 보여줘"
   - 도구: `get_report_detail(report_name=입찰공고 내역)` → 00607
   - 기대: conds·metrics·dims·링크. 부분명칭 매칭 실측.
4. "보고서 00033 상세 알려줘"
   - 도구: `get_report_detail(report_id=00033)`
   - 기대: 구매규격 사전공개 내역 1건 상세.
5. "수의계약 사유별 실적순위 보고서 찾아줘"
   - 도구: `search_reports` → 00620 지명
   - 기대: 00620/UI-ADOXFZ-021R + 건수 지표 명시.
6. "나라장터쇼핑몰 납품요구 물품 내역 조건 알려줘"
   - 도구: `get_report_detail(report_id=00118)`
   - 기대: 물품분류·세부품명·물품식별 팝업 조건 안내.

### 입력폼 (form_guide)

7. "00262 보고서에서 마감년월에 뭘 입력해야 해?"
   - 도구: `form_guide(report_id=00262)`
   - 기대: `yymmdate` + 연월 형식 + 전일(D-1) 기준. PASS 실측.
8. "00262 통계대상시스템 선택지 뭐야?"
   - 도구: `form_guide(report_id=00262)`
   - 기대: multiselect 허용값 10+ 나열 (나라장터 중앙/자체, 강원랜드, 학교장터…).
9. "수요기관 조건은 팝업이야 직접입력이야?"
   - 도구: `form_guide(report_id=00262)`
   - 기대: `popup(entity)` 명시 → B 연결 유도.
10. "00118 입력폼 안내해줘"
    - 도구: `form_guide(report_id=00118)`
    - 기대: 조건별 UI타입 목록.

### 값 확인 (value_lookup — 표본 후보 수준)

11. "00118 수요기관에 국방부 있어?"
    - 도구: `value_lookup(00118, 수요기관, 국방부)`
    - 기대: contains 2건 (제1군수지원사령부·제2작전사령부) + 표본注. PASS 실측.
12. "00118 업체명에 삼성전자 있어?"
    - 도구: `value_lookup(00118, 업체명, 삼성전자)`
    - 기대: exact/contains + 빈도(98건). PASS 패턴.
13. "00118 품명에 레미콘 있어?"
    - 도구: `value_lookup(00118, 품명, 레미콘)`
    - 기대: 443건 빈도付き 후보. 전수 아님 명시.
14. "00262 수요기관 조건에 한국전력 선택 가능해?"
    - 도구: `form_guide(00262)` + `value_lookup`
    - 기대: popup형이라 검색·선택형 + 통계대상시스템 옵션에 한국전력공사 존재. PASS 실측.

### 물품분류 (resolve / children / detail / products)

15. "레미콘 물품코드 알려줘"
    - 도구: `resolve_items(레미콘)` → `3011150501 exact`
    - 기대: 정규명+path+코드. PASS 실측 (total 3).
16. "노트북컴퓨터 분류코드 뭐야"
    - 도구: `resolve_items(노트북컴퓨터)` → `4321150301 exact`
    - 기대: path `43 > 4321 > 432115 > 43211503`. PASS 실측.
17. "의자 관련 분류 보여줘"
    - 도구: `resolve_items(의자)` → total 35
    - 기대: 전부 나열 금지, 대표 몇 + 되묻기 (사무용/치과용/특수). 좁히기 동작 PASS 실측.
18. "43으로 시작하는 품명 목록 보여줘"
    - 도구: `item_children(code=43)` → 품명 84개
    - 기대: 다음 분기점 나열. PASS 실측.
19. "43211501 하위 세부품명 뭐야"
    - 도구: `item_children(code=43211501)` → 터미널서버·컴퓨터서버
    - 기대: 2건 직접 반환. PASS 실측.
20. "3011150501 검증해줘"
    - 도구: `item_detail(code=3011150501)`
    - 기대: 레미콘 + path + 영문명 + 형제수. PASS 실측.
21. "레미콘 품목(식별번호) 예시 보여줘"
    - 도구: `item_products(code=3011150501)` → 10건
    - 기대: 토암·대명·한동 예시. 검색 불가는 명시. PASS 실측.

### 개념별 (concept_reports)

22. "여성기업 관련 보고서 다 보여줘"
    - 도구: `concept_reports(female_biz)` → 7건
    - 기대: 보고서ID+조건컬럼+role 표. PASS 실측.
23. "계약금액 보는 보고서 뭐 있어"
    - 도구: `concept_reports(contract_amount)`
    - 기대: 금액 지표 보고서 목록.
24. "수요기관 개념 들어간 보고서 목록"
    - 도구: `concept_reports(org_client)`
    - 기대: 수요기관 필터 보고서 목록.

### 가문·비교 (family_map)

25. "계약납품요구 보고서가 너무 많은데 뭐가 달라?"
    - 도구: `family_map(계약납품요구)` → 38건
    - 기대: 통계형 29+시각화 8 구분 + 대표 지명. PASS 실측.
26. "시각화 보고서만 골라줘"
    - 도구: `family_map` → `is_visual` 필터
    - 기대: 00168·00181·00243 등 시각화형만.
27. "00262랑 00260 뭐가 달라"
    - 도구: `family_map` + `get_report_detail` 2건
    - 기대: dims·조건 차분 (수요기관 1축 vs +계약방법 축).

### 신뢰도 (catalog_health)

28. "이 카탈로그 품질 어때"
    - 도구: `catalog_health(all)` → 131건 + rule 90/manual 41 + w2c 미적용 18건
    - 기대: 품질 단서. PASS 실측.
29. "00262 분류 확정된 거야"
    - 도구: `catalog_health` + `get_report_detail(00262)`
    - 기대: family_conf 단서 한 줄 ("분류 미확정"이면 유보).

### 체인 복합

30. "레미콘 조달 실적 알고 싶다"
    - 도구: `resolve_items(레미콘)` → `item_detail(3011150501)` → `search_reports(레미콘+코드)` → `get_report_detail(00030)`
    - 기대: 정규명+경로+코드+보고서 1건+링크. 4연타 PASS 실측.

## 한계 예시 5 (답이 안 나오거나 제한됨)

L1. "한전 총계약 건수 숫자 알려줘"
    - 막힘: 허브에 값 API 없음. 7툴 전부 메타·조건·목록 레벨.
    - 대응: 보고서+필터 안내까지. 숫자는 나라장터 화면에서 직접. 외부 계약정보 OpenAPI 영역.
L2. "신규 업체 ○○○ 등록됐어?"
    - 막힘: `value_lookup`은 19건 보고서 표본(n≈5000). "표본에 없다" ≠ "허브에 없다".
    - 대응: "후보" 표현 강제. 전수 API 교체 전까지 한계 유지.
L3. "작업용 의자 조달 실적 알려줘"
    - 막힘: upstream에 없는 표현 → resolve 0건. 재시도 쿼리도 어긋나면(`이자` 실측) 소모.
    - 대응: 되묻기 (사무용/산업용/모델). 억지 확정 금지.
L4. "2026년 신설 보고서 반영됐어?"
    - 막힘: 카탈로그 스냅샷 고정 (`w2d-20260919`). 신규 보고서·분류 변경 미반영.
    - 대응: `catalog_ver` 명시 + 빌드 주기 문서화. 실시간 필요하면 외부 영역.
L5. "차트 그려줘 / 엑셀 내려줘"
    - 막힘: 시각화 18건은 링크 안내로 종결. 렌더·파일 생성 범위 밖.
    - 대응: 보고서 링크 + 필터 안내. 이미지·파일 생성 안 함.
