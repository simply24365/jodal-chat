import { chatModels } from "@/lib/ai/models";

// Gateway neutralized: FastAPI owns the model. Single static entry so the
// selector renders without network calls.
export async function GET() {
  const headers = {
    "Cache-Control": "public, max-age=86400, s-maxage=86400",
  };
  const capabilities = Object.fromEntries(
    chatModels.map((m) => [m.id, { reasoning: false, tools: true, vision: false }])
  );
  return Response.json({ capabilities, models: chatModels }, { headers });
}
