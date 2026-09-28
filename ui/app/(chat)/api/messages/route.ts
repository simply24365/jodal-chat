// Auth/DB neutralized: FastAPI sessions own history. New chats start empty.
export async function GET(_request: Request) {
  return Response.json({
    isReadonly: false,
    messages: [],
    userId: null,
    visibility: "private",
  });
}
