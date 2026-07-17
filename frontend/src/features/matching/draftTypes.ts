export type ResumeDiffOp =
  | "reorder"
  | "rephrase"
  | "summarize"
  | "omit"
  | "highlight";

export interface ResumeDiffOpDetail {
  op: ResumeDiffOp;
  section: string;
  before: string | null;
  after: string | null;
  fact_ref: string;
  evidence_ids: string[];
}

export interface ResumeDraftResponse {
  id: string;
  match_report_id: string;
  job_title: string;
  company_name: string;
  diffs: ResumeDiffOpDetail[] | null;
  status: string;
  error_code: string | null;
  state_version: number;
  created_at: string;
  approved_at: string | null;
}

export interface AttachmentInfo {
  id: string;
  format: string;
  content_type: string;
  plaintext_size: number;
}

export interface ApprovedResumeVersionResponse {
  id: string;
  draft_id: string;
  approved_at: string;
  attachments: AttachmentInfo[];
}
