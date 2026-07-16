import { request } from "../../api";
import type { AdminFeedbackList, FeedbackCreatePayload, FeedbackList, JobFeedback } from "./feedbackTypes";

export function generateIdempotencyKey(): string {
  return crypto.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export const createFeedback = (
  token: string,
  payload: FeedbackCreatePayload,
  idempotencyKey?: string,
) => {
  const key = idempotencyKey || generateIdempotencyKey();
  return request<JobFeedback>("/feedbacks", {
    method: "POST",
    body: JSON.stringify(payload),
    headers: { "Idempotency-Key": key },
  }, token);
};

export const fetchFeedbacks = (token: string, jobId?: string, limit = 50, offset = 0) => {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (jobId) params.set("job_id", jobId);
  return request<FeedbackList>(`/feedbacks?${params.toString()}`, {}, token);
};

export const fetchFeedback = (token: string, feedbackId: string) =>
  request<JobFeedback>(`/feedbacks/${encodeURIComponent(feedbackId)}`, {}, token);

export const fetchAdminFeedbacks = (token: string, jobId?: string, limit = 50, offset = 0) => {
  const params = new URLSearchParams({ limit: String(limit), offset: String(offset) });
  if (jobId) params.set("job_id", jobId);
  return request<AdminFeedbackList>(`/admin/feedbacks?${params.toString()}`, {}, token);
};

export const fetchAdminFeedback = (token: string, feedbackId: string) =>
  request<JobFeedback>(`/admin/feedbacks/${encodeURIComponent(feedbackId)}`, {}, token);
