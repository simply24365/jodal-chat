import { chatModels } from "@/lib/ai/models";

// Gateway neutralized: FastAPI owns the model. Single static entry so the
// selector renders without network calls.
export async function GET() {
  // 캐시하지 않는다. 항목이 하나뿐이라 캐시 이득이 없는데 max-age 를 두면
  // 모델 이름·설정을 바꿔도 브라우저가 24시간 동안 옛 값을 붙잡고 있다
  // (실측 2026-09-30: 서버는 새 이름을 반환했는데 화면은 옛 이름이 떴다).
  const headers = {
    "Cache-Control": "no-store, must-revalidate",
  };
  const capabilities = Object.fromEntries(
    chatModels.map((m) => [m.id, { reasoning: false, tools: true, vision: false }])
  );
  return Response.json({ capabilities, models: chatModels }, { headers });
}
