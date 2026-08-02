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

export interface AgentPlanStepResponse {
  id: string
  objective: string
  allowed_skills: string[]
  success_criteria: string[]
  requires_verification: boolean
}

export interface AgentPlanResponse {
  id: string
  revision: number
  complexity: string
  success_criteria: string[]
  steps: AgentPlanStepResponse[]
  created_at: string
}

export interface AgentPlanListResponse {
  items: AgentPlanResponse[]
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
