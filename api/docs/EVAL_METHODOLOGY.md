# jodal-chat 평가·개선 방법론 (SSoT)

이 문서는 반복 개선 라운드에서 확립한 **측정 → 진단 → 구조 개선 → 재측정** 절차와,
각 개선이 어떤 근거로 채택/기각됐는지를 기록한다. 다음 세션은 여기서 이어간다.

## 1. 평가 파이프라인

```
pipeline/eval_judge.py  run      자연 질의 20건 → runs/<label>.ndjson (툴콜·검색결과 포함)
                        judge    루브릭 5기준 채점 → runs/<label>.judged.json
                        compare  두 run 비교(기준별 Δ, 교차표, 회귀/개선)
pipeline/eval_synth.py           카탈로그 → LLM 합성 질의 → gold 역검증
pipeline/build_doc2query.py      보고서 131건 → 예상질문·값별칭·synopsis 생성
pipeline/augment_docs.py         증강 문서 생성(원본 불변)
pipeline/test_regression.py      빠른 불변식 검사 10건 (LLM 불필요, <60초)
```

### 루브릭 (5기준, 모두 높을수록 좋음)
| 기준 | 값 | 판정 |
|---|---|---|
| goldcoverage | 0/1 | gold 보고서를 실제 전달(도메인밖이면 정직한 거절) |
| groundedness | 0/1 | 수치·ID·링크가 툴 기록에 근거 |
| toolappropriateness | 1-5 | 필요한 조회를 정확한 툴·인자로 |
| hygieneresidue | 0/1 | 미래형 약속·나레이션·think 태그·끊긴 링크 없음 |
| koreanquality | 0/1 | 자연스러운 한국어 |

### 저비용 장치 (토큰 절약)
- **프리필터**: 결정적으로 판정 가능한 위반(think 태그, 툴에 없는 ID)만 확정하고 judge 출력에서 제외
- **루브릭 압축**: 각 기준 한 줄로 한정
- **근거 시트**: 툴 반환 (ID|보고서명|synopsis) 전체 목록 — 절단된 원문만 주면 정상 인용을 환각으로 오판(실측 n01)
- **루브릭 버전 해시**: `judged.json.meta.rubric_version`. 이종 루브릭 compare는 차단.
- **편차 원인 규명**: 온도 0 xkiro는 동일 루브릭에서 3회 독립 채점 완전 일치(stdev=0).
  과거 관측된 ±0.14는 모델 비결정성이 아니라 **루브릭 프롬프트 변경**이었다.

## 2. 채택된 개선 (근거 포함)

| 개선 | 근거 | 효과 |
|---|---|---|
| doc2query 문서 증강 131건 | Doc2Query/InPars/RAGAS 계열. 어휘 갭으로 gold 미검색(n01) | hit@5 4→5/11 |
| 후보 synopsis·dims·views·query_sim | 64개 값 동률에서 에이전트 판단 근거 부족 | 후보 식별 개선 |
| catalog_facets | 메타질문("몇 개나?")에 집계 데이터 부재 | n03 정상 답변 |
| 인용 계약 통일(SearchDoc+citation_mapping+citable_reports) | jodal 툴만 citation 계약 부재 → 보고서명 환각 | 환각 차단 |
| 채널 정직화(value_lookup/detail 결과 병합) | 기관명 질의는 툴 체인이 정석 경로인데 검색만으로 판정 | gold 도달 2→3/11 |
| 빈 답 1회 재시도 + fallback 한국어화 | 툴 후 빈 답 → 영어 하드코딩 문구 노출 | koreanquality 0.75→0.85 |
| 무인자 툴 args_unparsed 오판 제거 | `{}`를 파싱 실패로 오판 → 3회 재생성 루프(n03) | 루프 해소 |
| search_reports 턴당 2회 상한 | 3회째부터 후보 불변 | 지연 35.6s→17s |

## 3. 측정 기반 기각 (채택하지 않음)

| 기법 | 실측 결과 | 이유 |
|---|---|---|
| RAG-Fusion (멀티쿼리 RRF) | hit@5 6/11 → 0/11 | 패러프레이즈가 도메인 고정 어휘(보고서명·조건명)를 파괴 |
| HyDE (가상 문서 임베딩) | hit@10 5/11 → 3/11 | e5-small이 가상문서 스타일과 실제 문서 사이 갭을 못 메움 |
| 쿼리 별칭 확장 | h@1 2/11 → 1/11 | 별칭 확장이 원 질의 신호를 희석 |

## 4. 설계 원칙 (사용자 지시 기반)

1. **heuristic/하드코딩 정렬 규칙을 추가하지 않는다** — 대신 에이전트의 판단 근거를 데이터로 제공한다
   (예: 정렬 대신 synopsis·dims·views·query_sim 노출)
2. **프롬프트 미세조정보다 데이터·구조를 바꾼다** — doc2query, 인용 계약, 페이로드 구조화
3. **de facto 기법은 리서치 후 측정으로 채택/기각** — 기각도 성과로 기록
4. **수십 분 걸리는 실행은 하지 않는다** — 오프라인/스모크 평가로 검증
5. **Windows·Oracle Linux 양쪽 동작** — .env 자체 로딩, resource 가드, LF 정규화

## 5. 남은 과제 (데이터 레이어 포화 지점)

- 값-보고서 역색인에 '기관명↔보고서' 직접 연결이 원천에 없다(예: 00262의 수요기관은 popup 조건).
  → value_lookup 스캔(64개 동률) + 에이전트 판단이 정석 경로이며, 검색 단독 해결은 실험으로 부적합 확인.
- 에이전트가 40개 후보 목록을 전부 활용하지 못함 — 멀티스텝 계획/자기검증은 프레임워크 레벨 과제.
- UI citation 승격·렌더 검증(브라우저), Oracle Linux 실배포 검증.
