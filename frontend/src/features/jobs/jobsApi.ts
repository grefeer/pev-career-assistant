import { request } from "../../api";
import type {
  AdminJobDetail,
  AdminJobListQuery,
  AdminJobListResponse,
  JobCompletionPayload,
  JobDecisionPayload,
  JobDetail,
  JobListQuery,
  JobListResponse,
  JobSourceKey,
  JobSyncResponse,
} from "./jobTypes";

export function fetchVerifiedJobs(
  token: string,
  query: JobListQuery,
): Promise<JobListResponse> {
  const params = new URLSearchParams({
    limit: String(query.limit),
    offset: String(query.offset),
  });
  if (query.company) params.set("company", query.company);
  if (query.recruitmentType) params.set("recruitment_type", query.recruitmentType);
  if (query.sourceKey) params.set("source_key", query.sourceKey);

  return request<JobListResponse>(`/jobs?${params.toString()}`, {}, token);
}

export function fetchVerifiedJob(token: string, jobId: string): Promise<JobDetail> {
  return request<JobDetail>(`/jobs/${encodeURIComponent(jobId)}`, {}, token);
}

export function fetchJobReviewQueue(
  token: string,
  query: AdminJobListQuery,
): Promise<AdminJobListResponse> {
  const params = new URLSearchParams({
    limit: String(query.limit),
    offset: String(query.offset),
  });
  if (query.reviewStatus) params.set("review_status", query.reviewStatus);
  return request<AdminJobListResponse>(
    `/admin/jobs/review-queue?${params.toString()}`,
    {},
    token,
  );
}

export function fetchAdminVerifiedJobs(
  token: string,
  query: Pick<AdminJobListQuery, "limit" | "offset">,
): Promise<AdminJobListResponse> {
  const params = new URLSearchParams({
    limit: String(query.limit),
    offset: String(query.offset),
  });
  return request<AdminJobListResponse>(`/admin/jobs/verified?${params.toString()}`, {}, token);
}

export function syncJobSource(token: string, sourceKey: JobSourceKey): Promise<JobSyncResponse> {
  return request<JobSyncResponse>(
    `/admin/job-sources/${encodeURIComponent(sourceKey)}/sync`,
    { method: "POST" },
    token,
  );
}

export function saveJobCompletion(
  token: string,
  jobId: string,
  payload: JobCompletionPayload,
): Promise<AdminJobDetail> {
  return request<AdminJobDetail>(`/admin/jobs/${encodeURIComponent(jobId)}/completion`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  }, token);
}

export function decideJob(
  token: string,
  jobId: string,
  payload: JobDecisionPayload,
): Promise<AdminJobDetail> {
  return request<AdminJobDetail>(`/admin/jobs/${encodeURIComponent(jobId)}/decision`, {
    method: "POST",
    body: JSON.stringify(payload),
  }, token);
}
