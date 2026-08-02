import { request } from "../../api";
import type {
  AdminFeedbackDecisionRequest, AdminFeedbackQueueResponse, FeedbackMutationRequest,
  FeedbackMutationResponse, JobFeedbackCategory, JobFeedbackStatus,
  StudentFeedbackListResponse,
} from "./jobFeedbackTypes";

export function generateFeedbackKey(): string {
  return crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2, 12)}`;
}

export const fetchMyJobFeedback = (token: string, jobId: string) =>
  request<StudentFeedbackListResponse>(`/jobs/${encodeURIComponent(jobId)}/feedback`, {}, token);

export const mutateJobFeedback = (
  token: string, jobId: string, payload: FeedbackMutationRequest, idempotencyKey: string,
) => request<FeedbackMutationResponse>(`/jobs/${encodeURIComponent(jobId)}/feedback`, {
  method: "POST", body: JSON.stringify(payload),
  headers: { "Idempotency-Key": idempotencyKey },
}, token);

export const fetchAdminJobFeedback = (
  token: string,
  filters: { status?: JobFeedbackStatus; category?: JobFeedbackCategory; limit?: number; offset?: number } = {},
) => {
  const params = new URLSearchParams();
  if (filters.status) params.set("status", filters.status);
  if (filters.category) params.set("category", filters.category);
  params.set("limit", String(filters.limit ?? 20));
  params.set("offset", String(filters.offset ?? 0));
  return request<AdminFeedbackQueueResponse>(`/admin/job-feedback?${params}`, {}, token);
};

export const decideJobFeedback = (
  token: string, feedbackId: string, payload: AdminFeedbackDecisionRequest,
  idempotencyKey: string,
) => request<FeedbackMutationResponse>(
  `/admin/job-feedback/${encodeURIComponent(feedbackId)}/decision`,
  { method: "POST", body: JSON.stringify(payload), headers: { "Idempotency-Key": idempotencyKey } },
  token,
);
