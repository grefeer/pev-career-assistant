export interface RequirementAssessment {
  requirement_id: string
  requirement: string
  job_field_path: string
  profile_field_path: string | null
  verdict: 'satisfied' | 'gap' | 'unknown'
  evidence_ids: string[]
  detail: string
}

export interface MatchReportResponse {
  id: string
  analysis_session_id: string
  job_id: string
  profile_version_id: string
  status: 'pending' | 'running' | 'completed' | 'failed'
  score: number | null
  score_components: Array<{ requirement_id: string; weight_basis_points: number; earned_basis_points: number }> | null
  strengths: RequirementAssessment[] | null
  gaps: RequirementAssessment[] | null
  unknowns: RequirementAssessment[] | null
  risks: RequirementAssessment[] | null
  application_priority: string | null
  recommendation: { text: string; requirement_ids: string[] } | null
  error_code: string | null
  scoring_rule_version: string
  model_version: string
  prompt_version: string
  output_schema_version: string
  created_at: string
  started_at: string | null
  completed_at: string | null
}

export interface JobOption {
  id: string
  title: string
  company_name: string
}

export interface ProfileVersionOption {
  id: string
  version_number: number
  created_at: string
}

export interface ResumeDraftResponse {
  id: string
  status: string
}
