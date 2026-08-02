import { request } from "../../api"
import type {
  AgentArtifactListResponse,
  AgentEventListResponse,
  AgentPlanListResponse,
  AgentRunCreatedResponse,
  AgentRunListResponse,
  AgentRunResponse,
  CreateAgentRunPayload,
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
