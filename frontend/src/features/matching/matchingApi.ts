import { request } from "../../api";
import type { MatchReportResponse, ResumeDraftResponse } from "./matchingTypes";

const BASE = "/matches";

function idempotencyKey(): string {
  return `match-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
}

export function createMatch(
  token: string,
  jobId: string,
  profileVersionId: string,
  sessionId?: string,
): Promise<MatchReportResponse> {
  return request<MatchReportResponse>(
    BASE,
    {
      method: "POST",
      body: JSON.stringify({
        job_id: jobId,
        profile_version_id: profileVersionId,
        analysis_session_id: sessionId || undefined,
      }),
      headers: { "Idempotency-Key": idempotencyKey() },
    },
    token,
  );
}

export function getMatch(
  token: string,
  matchId: string,
): Promise<MatchReportResponse> {
  return request<MatchReportResponse>(
    `${BASE}/${encodeURIComponent(matchId)}`,
    {},
    token,
  );
}

export function generateResumeDraft(
  token: string,
  matchId: string,
): Promise<ResumeDraftResponse> {
  return request<ResumeDraftResponse>(
    "/resume-drafts",
    {
      method: "POST",
      body: JSON.stringify({ match_id: matchId }),
    },
    token,
  );
}
