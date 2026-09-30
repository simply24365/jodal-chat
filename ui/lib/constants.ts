import { generateDummyPassword } from "./db/utils";

export const isProductionEnvironment = process.env.NODE_ENV === "production";
export const isDevelopmentEnvironment = process.env.NODE_ENV === "development";
export const isTestEnvironment = Boolean(
  process.env.PLAYWRIGHT_TEST_BASE_URL ||
    process.env.PLAYWRIGHT ||
    process.env.CI_PLAYWRIGHT
);

export const guestRegex = /^guest-\d+$/;

export const DUMMY_PASSWORD = generateDummyPassword();

// 빈 화면 제안 질문. upstream 데모 문구(Next.js·Dijkstra·Silicon Valley)를
// 조달데이터허브 의도로 교체했다. 4개가 의도적으로 서로 다른 조회 경로를 건드린다:
//   1. 보고서 검색        (search_reports)
//   2. 카탈로그 집계      (catalog_facets — 개수 질문)
//   3. 값 역색인          (value_lookup — 기관명·업체명)
//   4. 조회 폼 안내       (form_guide — 조건 지정법)
export const suggestions = [
  "기관별 계약납품요구 실적통계 보고서 있어?",
  "시각화할 수 있는 보고서는 몇 건이야?",
  "한국전력공사 실적은 어느 보고서로 봐?",
  "'수요기관' 조건으로 거르는 방법을 알려줘",
];
