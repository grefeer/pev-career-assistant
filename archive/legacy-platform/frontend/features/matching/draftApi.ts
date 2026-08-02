import { request } from "../../api";
import type {
  ApprovedResumeVersionResponse,
  ResumeDraftResponse,
} from "./draftTypes";

function draftIdempotencyKey(): string {
  return `draft-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export function createDraft(
  token: string,
  matchReportId: string,
): Promise<ResumeDraftResponse> {
  return request<ResumeDraftResponse>(
    "/resume-drafts",
    {
      method: "POST",
      body: JSON.stringify({ match_report_id: matchReportId }),
      headers: {
        "Idempotency-Key": draftIdempotencyKey(),
      } as Record<string, string>,
    },
    token,
  );
}

export function getDraft(
  token: string,
  draftId: string,
): Promise<ResumeDraftResponse> {
  return request<ResumeDraftResponse>(
    `/resume-drafts/${encodeURIComponent(draftId)}`,
    {},
    token,
  );
}

export function approveDraft(
  token: string,
  draftId: string,
  expectedVersion: number,
  idempotencyKey?: string,
): Promise<ApprovedResumeVersionResponse> {
  return request<ApprovedResumeVersionResponse>(
    `/resume-drafts/${encodeURIComponent(draftId)}/approve`,
    {
      method: "POST",
      body: JSON.stringify({ expected_version: expectedVersion }),
      headers: {
        "Idempotency-Key": idempotencyKey || draftIdempotencyKey(),
      } as Record<string, string>,
    },
    token,
  );
}

export function rejectDraft(
  token: string,
  draftId: string,
  expectedVersion: number,
): Promise<ResumeDraftResponse> {
  return request<ResumeDraftResponse>(
    `/resume-drafts/${encodeURIComponent(draftId)}/reject`,
    {
      method: "POST",
      body: JSON.stringify({ expected_version: expectedVersion }),
    },
    token,
  );
}

export function downloadAttachment(
  token: string,
  attachmentId: string,
  filename: string,
): Promise<void> {
  return new Promise<void>((resolve, reject) => {
    fetch(`/api/approved-resume-attachments/${attachmentId}/download`, {
      headers: { Authorization: `Bearer ${token}` },
    })
      .then((response) => {
        if (!response.ok) throw new Error("download failed");
        return response.blob();
      })
      .then((blob) => {
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
        resolve();
      })
      .catch(reject);
  });
}
