import { request } from "../../api";
import type { JobDetail, JobListQuery, JobListResponse } from "./jobTypes";

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
