import {
  createUIMessageStream,
  createUIMessageStreamResponse,
  generateId,
} from "ai";
import { ChatbotError } from "@/lib/errors";
import type {
  CitationData,
  ChatMessage,
  ToolActivityData,
} from "@/lib/types";
import { generateUUID } from "@/lib/utils";
import { type PostRequestBody, postRequestBodySchema } from "./schema";

export const maxDuration = 60;

const BACKEND_URL =
  process.env.CUSTOM_CHAT_BACKEND_URL ?? "http://127.0.0.1:8322";

type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

type BackendPacket = {
  type: string;
  turn_index?: number;
  tab_index?: number;
  data?: Record<string, JsonValue>;
  answer?: string;
  error?: string;
};

function textOfPart(part: unknown): string {
  if (!part || typeof part !== "object") return "";
  if (!("type" in part) || part.type !== "text") return "";
  if (!("text" in part)) return "";
  return typeof part.text === "string" ? part.text : "";
}

function toHistory(messages: ChatMessage[]) {
  return messages
    .flatMap((m) =>
      m.role === "user" || m.role === "assistant"
        ? [
            {
              content: (m.parts ?? []).map(textOfPart).join("").trim(),
              role: m.role,
            },
          ]
        : []
    )
    .filter((t) => t.content.length > 0)
    .slice(-20);
}

function toolNameFor(pkt: BackendPacket): string {
  const d = pkt.data ?? {};
  if (pkt.type.startsWith("search_tool")) return "web_search";
  if (pkt.type.startsWith("open_url")) return "open_url";
  return String(d.tool_name ?? "mcp");
}

// MCP tool_result 포장: stream 경험상 {"query_slots","candidates",...} 단일 포장.
// 구형 non-stream 응답은 {"tool_result": "<MCP JSON>"} 이중 포장이므로 둘 다 받는다.
// links.move를 가진 후보/detail을 찾아 citation 재료로 꺼낸다 (score 포함).
function extractMcpLinks(raw: JsonValue | undefined): {
  report_id: string;
  name: string;
  move: string;
  score: number;
}[] {
  if (typeof raw !== "string") return [];
  let first: unknown;
  try {
    first = JSON.parse(raw) as unknown;
  } catch {
    return [];
  }
  const innerText =
    first && typeof first === "object" && "tool_result" in first
      ? (first as { tool_result?: unknown }).tool_result
      : null;
  let payload: unknown = first;
  if (typeof innerText === "string") {
    try {
      payload = JSON.parse(innerText) as unknown;
    } catch {
      return [];
    }
  }
  if (!payload || typeof payload !== "object") return [];
  const out: { report_id: string; name: string; move: string; score: number }[] = [];
  const asRecord = (v: unknown): Record<string, unknown> | null =>
    v && typeof v === "object" && !Array.isArray(v)
      ? (v as Record<string, unknown>)
      : null;
  const pick = (rec: Record<string, unknown>): void => {
    const links = asRecord(rec.links);
    const move = links ? links.move : null;
    const rid = rec.report_id;
    if (typeof move === "string" && move && typeof rid === "string" && rid) {
      const name = typeof rec.name === "string" ? rec.name : rid;
      const score = typeof rec.stage_score === "number" ? rec.stage_score : 0;
      if (!out.some((o) => o.report_id === rid)) out.push({ report_id: rid, name, move, score });
    }
  };
  const root = asRecord(payload);
  if (!root) return [];
  const cands = root.candidates;
  if (Array.isArray(cands)) {
    for (const c of cands) {
      const rec = asRecord(c);
      if (rec) pick(rec);
    }
  } else {
    pick(root);
  }
  return out;
}

export async function POST(request: Request) {
  let requestBody: PostRequestBody;

  try {
    const json = await request.json();
    requestBody = postRequestBodySchema.parse(json);
  } catch {
    return new ChatbotError("bad_request:api").toResponse();
  }

  const { id, message, messages } = requestBody;
  const lastUser = message?.role === "user" ? message : null;
  const convo = (messages ?? []) as ChatMessage[];
  // 클라이언트가 전체 대화를 함께 보내면 그걸 기준으로 한다. `message` 하나만
  //으로는 slice(0, -1) 이 항상 비어서(길이 1 배열) 과거 대화가 FastAPI에 한 번도
  // 전달되지 않았다 — "그럼 2025년으로" 같은 후속질의가 전부 무맥락이 되던 원인.
  const incoming: ChatMessage[] = convo.length
    ? convo
    : lastUser
      ? [lastUser as ChatMessage]
      : [];
  const latestText = [...incoming]
    .reverse()
    .flatMap((m) =>
      m.role === "user" ? [(m.parts ?? []).map(textOfPart).join("").trim()] : []
    )
    .find((t) => t.length > 0);

  if (!latestText) {
    return new ChatbotError("bad_request:api").toResponse();
  }

  // convo의 마지막이 이번 턴이므로 하나를 제외해 과거로 삼는다.
  const history = toHistory(convo.length ? incoming.slice(0, -1) : []);

  const stream = createUIMessageStream<ChatMessage>({
    execute: async ({ writer: dataStream }) => {
      const textId = generateUUID();
      const reasoningId = generateUUID();
      let textOpen = false;
      let reasoningOpen = false;
      const toolBlocks = new Map<string, ToolActivityData & { id: string }>();
      // MCP 후보 캐시: search_reports 10건×N회 호출분을 즉시 승격하면 버튼이 쏟아진다.
      // 캐시에만 적재하고, 스트림 종료 후 최종 답에 지명된 1~3건만 data-citation으로 승격.
      const mcpHits = new Map<string, { report_id: string; name: string; move: string; score: number }>();
      const flushTool = (key: string) => {
        const b = toolBlocks.get(key);
        if (!b) return;
        toolBlocks.delete(key);
        dataStream.write({ data: { ...b }, type: "data-tool-activity" });
      };

      const toolBlock = (pkt: BackendPacket): ToolActivityData & { id: string } => {
        const key = `${pkt.turn_index ?? 0}-${pkt.tab_index ?? 0}-${toolNameFor(pkt)}`;
        let b = toolBlocks.get(key);
        if (!b) {
          b = {
            id: generateId(),
            name: toolNameFor(pkt),
            queries: [],
            result: "",
            tab: pkt.tab_index ?? 0,
            turn: pkt.turn_index ?? 0,
            urls: [],
          };
          toolBlocks.set(key, b);
        }
        return b;
      };

      let res: Response;
      try {
        res = await fetch(`${BACKEND_URL}/chat`, {
          body: JSON.stringify({
            history,
            message: latestText,
            session_id: id,
            stream: true,
            web_search: true,
          }),
          headers: { "Content-Type": "application/json" },
          method: "POST",
          signal: request.signal,
        });
      } catch (error) {
        dataStream.write({
          delta: `Backend unreachable at ${BACKEND_URL}: ${error instanceof Error ? error.message : String(error)}`,
          id: generateId(),
          type: "text-delta",
        });
        return;
      }

      if (!res.ok || !res.body) {
        dataStream.write({
          delta: `Backend HTTP ${res.status}`,
          id: generateId(),
          type: "text-delta",
        });
        return;
      }

      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      let streamed = "";

      const emitText = (delta: string) => {
        if (!delta) return;
        if (!textOpen) {
          dataStream.write({ id: textId, type: "text-start" });
          textOpen = true;
        }
        streamed += delta;
        dataStream.write({ delta, id: textId, type: "text-delta" });
      };

      const emitReasoning = (delta: string) => {
        if (!delta) return;
        if (!reasoningOpen) {
          dataStream.write({ id: reasoningId, type: "reasoning-start" });
          reasoningOpen = true;
        }
        dataStream.write({ delta, id: reasoningId, type: "reasoning-delta" });
      };

      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx: number;
        while ((idx = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, idx).trim();
          buf = buf.slice(idx + 1);
          if (!line) continue;
          let pkt: BackendPacket;
          try {
            pkt = JSON.parse(line) as BackendPacket;
          } catch {
            continue;
          }
          const d = pkt.data ?? {};
          switch (pkt.type) {
            case "reasoning_start":
              break;
            case "reasoning_delta":
              emitReasoning(String(d.reasoning ?? ""));
              break;
            case "reasoning_done":
              if (reasoningOpen) {
                dataStream.write({ id: reasoningId, type: "reasoning-end" });
                reasoningOpen = false;
              }
              break;
            case "message_delta":
              emitText(String(d.content ?? ""));
              break;
            case "search_tool_start":
            case "open_url_start":
            case "custom_tool_start":
              toolBlock(pkt);
              break;
            case "search_tool_queries_delta": {
              const b = toolBlock(pkt);
              b.queries = Array.isArray(d.queries) ? d.queries.map(String) : [];
              break;
            }
            case "open_url_urls_delta": {
              const b = toolBlock(pkt);
              b.urls = Array.isArray(d.urls) ? d.urls.map(String) : [];
              break;
            }
            case "search_tool_documents_delta":
            case "open_url_documents_delta": {
              const b = toolBlock(pkt);
              const docs = Array.isArray(d.documents) ? d.documents : [];
              const kind = pkt.type.startsWith("open_url") ? "page(s)" : "result(s)";
              b.result = `${docs.length} ${kind}: ` + docs.slice(0, 3).map((x: any) => x?.title).join(" · ");
              break;
            }
            case "custom_tool_args": {
              const b = toolBlock(pkt);
              b.result = "";
              try {
                b.queries = [JSON.stringify(d.tool_args ?? {}).slice(0, 300)];
              } catch {
                b.queries = [];
              }
              break;
            }
            case "custom_tool_delta": {
              const b = toolBlock(pkt);
              const s = d.tool_result !== undefined ? String(d.tool_result) : JSON.stringify(d).slice(0, 500);
              b.result = s.slice(0, 500);
              // 후보는 캐시에만 적재. citation 승격은 스트림 종료 후 최종 답 지명분만.
              for (const hit of extractMcpLinks(d.tool_result)) {
                const prev = mcpHits.get(hit.report_id);
                if (!prev || hit.score > prev.score) mcpHits.set(hit.report_id, hit);
              }
              if (mcpHits.size) b.result = `후보 ${mcpHits.size}건 수집 중 (최종 답에 지명된 것만 링크 표시)`;
              break;
            }
            case "citation_info": {
              const strOrNull = (v: unknown): string | null =>
                typeof v === "string" ? v : null;
              const citation: CitationData = {
                document_id: strOrNull(d.document_id),
                link: strOrNull(d.link),
                number: Number(d.citation_number ?? 0),
                title: strOrNull(d.title),
              };
              dataStream.write({ data: citation, type: "data-citation" });
              break;
            }
            case "section_end":
              flushTool(`${pkt.turn_index ?? 0}-${pkt.tab_index ?? 0}-${toolNameFor(pkt)}`);
              break;
            case "message_start":
            case "stop":
              break;
            case "error":
              emitText(`\n\nerror: ${String(d.error ?? pkt.error ?? "unknown")}`);
              break;
            case "final":
              break;
            default:
              break;
          }
        }
      }

      // 최종 답에 지명된 보고서만 citation 승격 (최대 3).
      // 1순위: 본문에 [markdown 링크](url)로 걸린 move URL. 2순위: 5자리 ID 언급. 둘 다 없으면 score 상위 2건.
      {
        const linkedMoves = new Set<string>();
        for (const m of streamed.matchAll(/\[[^\]]*\]\((https:\/\/data\.g2b\.go\.kr\/link\/[^)]+)\)/g)) {
          linkedMoves.add(m[1]);
        }
        const byMove = new Map([...mcpHits.values()].map((h) => [h.move, h]));
        const linked = [...linkedMoves].map((u) => byMove.get(u)).filter((h) => h !== undefined);
        const ids = [...new Set((streamed.match(/\b\d{5}\b/g) ?? []).filter((id) => mcpHits.has(id)))].slice(0, 3);
        const picks = linked.length
          ? linked.slice(0, 3).map((h) => h.report_id)
          : ids.length
            ? ids
            : [...mcpHits.values()].sort((a, b) => b.score - a.score).slice(0, 2).map((h) => h.report_id);
        for (const rid of picks) {
          const hit = mcpHits.get(rid);
          if (!hit) continue;
          const citation: CitationData = {
            document_id: hit.report_id,
            link: hit.move,
            number: 0,
            // report_id(00262)는 내부 식별자다. 사용자에게는 보고서명만 보여준다.
            title: hit.name,
          };
          dataStream.write({ data: citation, type: "data-citation" });
        }
      }
      for (const key of [...toolBlocks.keys()]) flushTool(key);
      if (reasoningOpen) {
        dataStream.write({ id: reasoningId, type: "reasoning-end" });
      }
      if (textOpen) {
        dataStream.write({ id: textId, type: "text-end" });
      }
    },
    generateId: generateUUID,
    onError: () => "Oops, an error occurred!",
  });

  return createUIMessageStreamResponse({ stream });
}

export async function DELETE() {
  return Response.json({ deleted: 0 }, { status: 200 });
}
