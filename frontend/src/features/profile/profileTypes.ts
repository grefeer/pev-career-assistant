export type EvidenceDecisionPayload =
  | { evidence_id: string; action: "confirm"; corrected_value: null }
  | { evidence_id: string; action: "ignore"; corrected_value: null }
  | { evidence_id: string; action: "correct"; corrected_value: unknown };

export interface ProfileEvidence {
  id: string;
  resume_import_id: string;
  field_path: string;
  candidate_value: unknown;
  evidence_excerpt: string;
  confidence: number;
  status: "pending" | "confirmed" | "corrected" | "ignored";
  diff_action: "add" | "replace" | "unchanged" | "conflict";
}

export interface ResumeAssetMetadata {
  id: string;
  original_filename: string;
  content_type: string;
  plaintext_size: number;
  encryption_version: string;
  status: string;
  error_code: string | null;
  created_at: string;
  updated_at: string;
}

export interface ResumeImportDetail {
  id: string;
  asset_id: string;
  parser_version: string;
  status: string;
  error_code: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface ProfileDetail {
  id: string;
  version: number;
  evidence: ProfileEvidence[];
  local_sensitive_references: Record<string, unknown>;
  latest_version: { id: string; version_number: number; created_at: string } | null;
}

export interface ConfirmedProfileVersionSummary {
  id: string;
  version_number: number;
  aggregate_version: number;
  created_at: string;
}

export interface ConfirmedProfileVersionDetail {
  id: string;
  version_number: number;
  aggregate_version: number;
  facts_snapshot: Record<string, unknown>;
  evidence_refs: Record<string, unknown>;
  local_sensitive_references: Record<string, unknown>;
  created_at: string;
}
