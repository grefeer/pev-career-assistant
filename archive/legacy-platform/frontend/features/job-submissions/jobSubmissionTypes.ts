export type SubmissionInputType = "url" | "jd_text";
export type SubmissionStatus = "draft" | "submitted" | "promoted" | "rejected";
export type DeduplicationStatus = "pending" | "succeeded" | "failed";

export interface JobSubmission {
  id: string;
  input_type: SubmissionInputType;
  input_preview: string;
  normalized_url: string | null;
  status: SubmissionStatus;
  version: number;
  deduplication_status: DeduplicationStatus;
  deduplication_error_code: string | null;
  promoted_job_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface AdminJobSubmission extends JobSubmission { content_sha256: string }
export interface JobSubmissionList { total: number; submissions: JobSubmission[] }
export interface AdminJobSubmissionList { total: number; submissions: AdminJobSubmission[] }

export interface DuplicateCandidate {
  job: { id: string; company_name: string; title: string; status: string; apply_url: string };
  score_basis_points: number;
  reasons: string[];
  score_components: Record<string, number>;
  algorithm_version: "manual-job-dedup-v1";
}

export type JobSubmissionCreate =
  | { input_type: "url"; url: string; jd_text?: never }
  | { input_type: "jd_text"; jd_text: string; url?: never };

export type AdminJobSubmissionDecision =
  | { expected_version: number; action: "link_existing"; job_id: string }
  | {
      expected_version: number;
      action: "create_pending";
      company_name: string;
      title: string;
      apply_url?: string;
    }
  | {
      expected_version: number;
      action: "reject";
      reason_code: "not_a_job" | "insufficient_evidence" | "unsafe_link" | "duplicate_submission";
    };
