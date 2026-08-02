import { ApiError, request } from "../../api"
import type {
  AgentArtifactListResponse,
  AgentEventListResponse,
  AgentPlanListResponse,
  AgentRunCreatedResponse,
  AgentRunListResponse,
  AgentRunResponse,
  CreateAgentRunPayload,
  AgentEventResponse,
} from "./agentRuntimeTypes"

const BASE = "/agent-runs"

export function createAgentRun(
  token: string,
  payload: CreateAgentRunPayload,
): Promise<AgentRunCreatedResponse> {
  const candidateUrls = (payload.candidate_urls ?? [])
    .map((url) => url.trim())
    .filter(Boolean)
  return request<AgentRunCreatedResponse>(
    BASE,
    {
      method: "POST",
      body: JSON.stringify({
        goal: payload.goal,
        allowed_skills: payload.allowed_skills,
        context: candidateUrls.length ? { candidate_urls: candidateUrls } : {},
      }),
    },
    token,
  )
}

export function resumeAgentRun(
  token: string,
  runId: string,
  userResponse: string,
): Promise<AgentRunCreatedResponse> {
  return request<AgentRunCreatedResponse>(
    `${BASE}/${encodeURIComponent(runId)}/resume`,
    {
      method: "POST",
      body: JSON.stringify({ user_response: userResponse.trim() }),
    },
    token,
  )
}

export function recoverAgentRun(
  token: string,
  runId: string,
): Promise<AgentRunCreatedResponse> {
  return request<AgentRunCreatedResponse>(
    `${BASE}/${encodeURIComponent(runId)}/recover`,
    {
      method: "POST",
      body: JSON.stringify({}),
    },
    token,
  )
}

export function fetchAgentRuns(token: string, limit = 20): Promise<AgentRunListResponse> {
  return request<AgentRunListResponse>(`${BASE}?limit=${limit}`, {}, token)
}

export function fetchAgentRun(token: string, runId: string): Promise<AgentRunResponse> {
  return request<AgentRunResponse>(`${BASE}/${encodeURIComponent(runId)}`, {}, token)
}

export function fetchAgentRunEvents(
  token: string,
  runId: string,
): Promise<AgentEventListResponse> {
  return request<AgentEventListResponse>(
    `${BASE}/${encodeURIComponent(runId)}/events`,
    {},
    token,
  )
}

function parseSseEvent(block: string): AgentEventResponse | null {
  const lines = block.split(/\r?\n/)
  const data = lines.find((line) => line.startsWith("data: "))?.slice(6)
  if (!data) return null
  try {
    const parsed: unknown = JSON.parse(data)
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null
    const event = parsed as Record<string, unknown>
    if (
      typeof event.sequence !== "number"
      || typeof event.event_type !== "string"
      || !event.payload
      || typeof event.payload !== "object"
      || Array.isArray(event.payload)
      || typeof event.created_at !== "string"
    ) return null
    // All required fields are validated above, so the narrow cast is sound.
    return event as unknown as AgentEventResponse
  } catch {
    return null
  }
}

/**
 * Consume an authenticated, replayable SSE response with an explicit durable
 * cursor. Browser EventSource cannot send our Bearer token, so this uses fetch.
 */
export async function streamAgentRunEvents(
  token: string,
  runId: string,
  afterSequence: number,
  signal: AbortSignal | undefined,
  onEvent: (event: AgentEventResponse) => void,
): Promise<void> {
  const response = await fetch(
    `/api${BASE}/${encodeURIComponent(runId)}/events/stream?after_sequence=${afterSequence}`,
    { headers: { Authorization: `Bearer ${token}` }, signal },
  )
  if (!response.ok) {
    throw new ApiError(response.status, null, `请求失败：${response.status}`)
  }
  if (!response.body) {
    throw new ApiError(response.status, null, "事件流不可用")
  }
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ""
  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value ?? new Uint8Array(), { stream: !done })
    const blocks = buffer.split(/\r?\n\r?\n/)
    // ``String.split`` always yields a non-empty array, so ``pop()`` is
    // always a string here (never ``undefined``) and needs no fallback.
    buffer = blocks.pop()!
    for (const block of blocks) {
      const event = parseSseEvent(block)
      if (event) onEvent(event)
    }
    if (done) return
  }
}

export function fetchAgentRunPlans(
  token: string,
  runId: string,
): Promise<AgentPlanListResponse> {
  return request<AgentPlanListResponse>(
    `${BASE}/${encodeURIComponent(runId)}/plans`,
    {},
    token,
  )
}

export function fetchAgentRunArtifacts(
  token: string,
  runId: string,
): Promise<AgentArtifactListResponse> {
  return request<AgentArtifactListResponse>(
    `${BASE}/${encodeURIComponent(runId)}/artifacts`,
    {},
    token,
  )
}
