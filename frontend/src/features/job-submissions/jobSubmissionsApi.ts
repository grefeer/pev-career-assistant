import { request } from "../../api";
import type {
  AdminJobSubmission,
  AdminJobSubmissionDecision,
  AdminJobSubmissionList,
  DuplicateCandidate,
  JobSubmission,
  JobSubmissionCreate,
  JobSubmissionList,
} from "./jobSubmissionTypes";

export const fetchJobSubmissions = (token: string, limit = 20, offset = 0) =>
  request<JobSubmissionList>(`/job-submissions?limit=${limit}&offset=${offset}`, {}, token);

export const createJobSubmission = (token: string, payload: JobSubmissionCreate) =>
  request<JobSubmission>("/job-submissions", { method: "POST", body: JSON.stringify(payload) }, token);

export const updateJobSubmission = (
  token: string, id: string, expectedVersion: number, payload: JobSubmissionCreate,
) => request<JobSubmission>(`/job-submissions/${encodeURIComponent(id)}`, {
  method: "PATCH", body: JSON.stringify({ ...payload, expected_version: expectedVersion }),
}, token);

export const submitJobSubmission = (token: string, id: string, expectedVersion: number) =>
  request<JobSubmission>(`/job-submissions/${encodeURIComponent(id)}/submit`, {
    method: "POST", body: JSON.stringify({ expected_version: expectedVersion }),
  }, token);

export const fetchDuplicateCandidates = (token: string, id: string) =>
  request<{ candidates: DuplicateCandidate[] }>(
    `/job-submissions/${encodeURIComponent(id)}/duplicate-candidates`, {}, token,
  );

export const fetchAdminJobSubmissions = (token: string, limit = 20, offset = 0) =>
  request<AdminJobSubmissionList>(
    `/admin/job-submissions?status=submitted&limit=${limit}&offset=${offset}`, {}, token,
  );

export const decideJobSubmission = (
  token: string, id: string, payload: AdminJobSubmissionDecision,
) => request<AdminJobSubmission>(`/admin/job-submissions/${encodeURIComponent(id)}/decision`, {
  method: "POST", body: JSON.stringify(payload),
}, token);
