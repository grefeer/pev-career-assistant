export type JobStatus =
  | "pending_completion"
  | "pending_review"
  | "verified"
  | "expired"
  | "rejected";

export interface JobSummary {
  id: string;
  company_name: string;
  title: string;
  locations: string[];
  recruitment_types: string[];
  industries: string[];
  apply_url: string;
  deadline_text: string | null;
  status: JobStatus;
  gui_eligible: boolean;
  source_key: string;
  source_name: string;
  updated_at: string;
}

export interface JobDetail extends JobSummary {
  description_text: string;
  referral_code: string | null;
  verified_at: string | null;
}

export interface JobListResponse {
  total: number;
  jobs: JobSummary[];
}

export interface JobListQuery {
  limit: number;
  offset: number;
  company?: string;
  recruitmentType?: string;
  sourceKey?: string;
}

export type ReviewQueueStatus = "pending_completion" | "pending_review" | "rejected";

export interface JobSourceCandidate {
  company_name: string | null;
  title: string | null;
  locations: string[];
  recruitment_types: string[];
  industries: string[];
  apply_url: string | null;
  referral_code: string | null;
  deadline_text: string | null;
}

export interface AdminJobDetail extends JobSummary {
  description_text: string | null;
  referral_code: string | null;
  source_candidate: JobSourceCandidate;
  source_changed_since_review: boolean;
  review_version: number;
}

export interface AdminJobListResponse {
  total: number;
  jobs: AdminJobDetail[];
}

export interface AdminJobListQuery {
  limit: number;
  offset: number;
  reviewStatus?: ReviewQueueStatus;
}

export type JobSourceKey = "tencent-27-referrals" | "tencent-intern-referrals";

export interface JobSyncResponse {
  run_id: string;
  source_key: JobSourceKey;
  status: "succeeded" | "failed" | "running";
  pages_read: number;
  records_read: number;
  raw_snapshots_created: number;
  postings_created: number;
  postings_updated: number;
  records_skipped_incomplete: number;
  started_at: string;
  finished_at: string;
}

// ── Job Discovery (Phase 7) ─────────────────────────────────────────────

export interface JobDiscoveryTask {
  id: string;
  source_key: string;
  source_name: string | null;
  source_url: string;
  status: string;
  block_reason: string | null;
  attempt_count: number;
  result_summary_json: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
}

export interface JobDiscoveryTaskListResponse {
  tasks: JobDiscoveryTask[];
}

export interface DiscoveredJobCandidate {
  id: string;
  task_id: string;
  similarity_group_key: string;
  status: string;
  title: string | null;
  company_name: string | null;
  description_text: string | null;
  locations_json: string[] | null;
  apply_url: string | null;
  confidence: number | null;
  evidence_refs_json: Array<{ url?: string; title?: string; excerpt?: string; type?: string }> | null;
  normalization_warnings_json: string[] | null;
  created_at: string;
}

export interface JobDiscoveryReviewGroup {
  similarity_group_key: string;
  candidates: DiscoveredJobCandidate[];
}

export interface JobCompletionPayload {
  expected_version: number;
  company_name: string;
  title: string;
  description_text: string;
  locations: string[];
  recruitment_types: string[];
  industries: string[];
  apply_url: string;
  referral_code: string | null;
  deadline_text: string | null;
}

interface JobDecisionPayloadBase {
  expected_version: number;
  gui_eligible: boolean;
}

export type RejectReasonCode =
  | "invalid_source"
  | "wrong_company"
  | "insufficient_job_details"
  | "unsafe_or_invalid_apply_channel";

export type ExpireReasonCode =
  | "closed_on_official_site"
  | "deadline_passed"
  | "application_channel_unavailable";

export type JobDecisionPayload =
  | (JobDecisionPayloadBase & {
      decision: "verify";
      reason_code: null;
    })
  | (JobDecisionPayloadBase & {
      decision: "reject";
      gui_eligible: false;
      reason_code: RejectReasonCode;
    })
  | (JobDecisionPayloadBase & {
      decision: "expire";
      gui_eligible: false;
      reason_code: ExpireReasonCode;
    });
