"use server";

import type { UIMessage } from "ai";
import { cookies } from "next/headers";
import type { VisibilityType } from "@/components/chat/visibility-selector";
import { getTextFromMessage } from "@/lib/utils";

export async function saveChatModelAsCookie(model: string) {
  const cookieStore = await cookies();
  cookieStore.set("chat-model", model);
}

export async function generateTitleFromUserMessage({
  message,
}: {
  message: UIMessage;
}) {
  // Gateway neutralized: derive title from first user text, no LLM call.
  const text = getTextFromMessage(message).trim().replace(/\s+/g, " ");
  return text.slice(0, 60) || "New chat";
}

export async function deleteTrailingMessages({ id }: { id: string }) {
  // Auth/DB neutralized: message edits re-send from browser state.
  console.debug("deleteTrailingMessages noop", id);
}

export async function updateChatVisibility({
  chatId,
  visibility,
}: {
  chatId: string;
  visibility: VisibilityType;
}) {
  // Auth/DB neutralized: visibility is browser-local.
  console.debug("updateChatVisibility noop", chatId, visibility);
}
