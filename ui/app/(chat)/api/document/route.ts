// Auth/DB neutralized: artifact documents live in browser state only.

export async function GET() {
  return Response.json([], { status: 200 });
}

export async function POST() {
  return Response.json([], { status: 200 });
}

export async function PATCH() {
  return new Response("noop", { status: 200 });
}

export async function DELETE() {
  return new Response("noop", { status: 200 });
}
