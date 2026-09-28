import type { NextRequest } from "next/server";

// Auth/DB neutralized: history lives in FastAPI sessions + browser state.
// Sidebar calls useSWRInfinite with user=undefined -> key () => null, so this
// route is normally never hit. Keep a shape-compatible stub for safety.
export async function GET(_request: NextRequest) {
  return Response.json({ chats: [], hasMore: false });
}

export async function DELETE() {
  return Response.json({ deleted: 0 }, { status: 200 });
}

/* bulk delete neutralized: no server-side history */
