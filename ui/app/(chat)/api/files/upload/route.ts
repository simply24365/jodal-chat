// Auth/blob neutralized: attachments disabled (FastAPI is text-only).
import { NextResponse } from "next/server";

export async function POST() {
  return NextResponse.json({ error: "Attachments disabled" }, { status: 400 });
}
