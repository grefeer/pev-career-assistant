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
