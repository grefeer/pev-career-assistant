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

export interface JobDecisionPayload {
  expected_version: number;
  decision: "verify" | "reject" | "expire";
  gui_eligible: boolean;
  reason_code: string | null;
}
