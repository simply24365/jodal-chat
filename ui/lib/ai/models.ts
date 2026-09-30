export const DEFAULT_CHAT_MODEL = "custom-chat/fastapi";
// 표시 이름은 제품명. id("custom-chat/fastapi")는 프로토콜 식별자라 그대로 둔다 —
// DEFAULT_CHAT_MODEL 과 전송 payload 가 이 값을 참조한다.
export const CHAT_MODEL_DISPLAY_NAME = "조달데이터허브 AI";

export const titleModel = {
  description: "Unused: FastAPI owns titles",
  id: "custom-chat/fastapi",
  name: CHAT_MODEL_DISPLAY_NAME,
  provider: "custom-chat",
};

export type ModelCapabilities = {
  tools: boolean;
  vision: boolean;
  reasoning: boolean;
};

export type ChatModel = {
  id: string;
  name: string;
  provider: string;
  description: string;
  gatewayOrder?: string[];
  reasoningEffort?: "none" | "minimal" | "low" | "medium" | "high";
};

export const chatModels: ChatModel[] = [
  {
    description: "FastAPI agent loop (custom_chat backend)",
    id: "custom-chat/fastapi",
    name: CHAT_MODEL_DISPLAY_NAME,
    provider: "custom-chat",
  },
];

// Gateway neutralized: FastAPI owns model + capabilities. No network.
export async function getCapabilities(): Promise<
  Record<string, ModelCapabilities>
> {
  return Object.fromEntries(
    chatModels.map((model) => [
      model.id,
      { reasoning: false, tools: true, vision: false },
    ])
  );
}

export const isDemo = process.env.IS_DEMO === "1";

type GatewayModel = {
  id: string;
  name: string;
  type?: string;
  tags?: string[];
};

export type GatewayModelWithCapabilities = ChatModel & {
  capabilities: ModelCapabilities;
};

export async function getAllGatewayModels(): Promise<
  GatewayModelWithCapabilities[]
> {
  return [];
}

export function getActiveModels(): ChatModel[] {
  return chatModels;
}

export const allowedModelIds = new Set(chatModels.map((m) => m.id));

export type ModelAvailability = "healthy" | "impacted" | "unknown";

type GatewayEndpoint = {
  provider_name?: string;
  status?: number;
  uptime_last_15m?: number;
  uptime_last_1h?: number;
  latency_last_1h?: {
    p50?: number;
    p95?: number;
  };
};
const PROVIDER_IMPACTED_UPTIME_THRESHOLD = 99;
const PROVIDER_IMPACTED_P50_MS = 10_000;
const PROVIDER_IMPACTED_P95_MS = 30_000;

function isEndpointImpacted(endpoint: GatewayEndpoint) {
  return (
    (endpoint.status !== undefined && endpoint.status !== 0) ||
    (endpoint.uptime_last_15m !== undefined &&
      endpoint.uptime_last_15m < PROVIDER_IMPACTED_UPTIME_THRESHOLD) ||
    (endpoint.uptime_last_1h !== undefined &&
      endpoint.uptime_last_1h < PROVIDER_IMPACTED_UPTIME_THRESHOLD) ||
    (endpoint.latency_last_1h?.p50 !== undefined &&
      endpoint.latency_last_1h.p50 > PROVIDER_IMPACTED_P50_MS) ||
    (endpoint.latency_last_1h?.p95 !== undefined &&
      endpoint.latency_last_1h.p95 > PROVIDER_IMPACTED_P95_MS)
  );
}

export async function getModelAvailability(
  _modelId: string
): Promise<ModelAvailability> {
  return "healthy";
}

/* availability resolved statically; see getModelAvailability above */
