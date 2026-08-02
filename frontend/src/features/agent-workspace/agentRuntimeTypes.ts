export type CareerSkillName =
  | "job-discovery"
  | "job-matching"
  | "resume-tailoring"
  | "career-planning"

export interface CreateAgentRunPayload {
  goal: string
  allowed_skills: CareerSkillName[]
  candidate_urls?: string[]
}

export interface AgentRunCreatedResponse {
  id: string
  status: string
  summary: string | null
  error_code: string | null
}

export interface AgentRunResponse {
  id: string
  goal: string
  status: string
  complexity: string | null
  summary: string | null
  error_code: string | null
  created_at: string
  updated_at: string
}

export interface AgentRunListResponse {
  items: AgentRunResponse[]
}

export interface AgentEventResponse {
  sequence: number
  event_type: string
  payload: Record<string, unknown>
  created_at: string
}

export interface AgentEventListResponse {
  items: AgentEventResponse[]
}

export interface AgentArtifactResponse {
  id: string
  artifact_type: string
  source_url: string
  content_hash: string
  content: Record<string, unknown>
  created_at: string
}

export interface AgentArtifactListResponse {
  items: AgentArtifactResponse[]
}
