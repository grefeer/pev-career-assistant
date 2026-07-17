export interface SnapshotSummary {
  id: string;
  job_id: string;
  approved_resume_version_id: string;
  profile_version_id: string;
  company_name: string;
  title: string;
  gui_eligible: boolean;
  job_status_at_snapshot: string;
  job_review_version_at_snapshot: number;
  created_at: string;
  schema_version: string;
}

export interface SnapshotListResponse {
  items: SnapshotSummary[];
  total: number;
}

export interface CreateSnapshotRequest {
  job_id: string;
  approved_resume_version_id: string;
  dynamic_answers: Record<string, unknown>[];
  local_sensitive_requirements: Record<string, unknown>[];
}

export interface TaskEligibilityResponse {
  can_create_task: boolean;
  reason_code: string | null;
}

export interface CreateTaskResponse {
  task_id: string;
  snapshot_id: string;
  status: string;
  state_version: number;
}
