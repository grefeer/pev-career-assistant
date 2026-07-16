import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createJobSubmission,
  decideJobSubmission,
  fetchDuplicateCandidates,
  submitJobSubmission,
} from "../jobSubmissionsApi";

describe("job submissions API", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("uses private student routes and encodes ids", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(
      new Response(JSON.stringify({ id: "submission/1" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ));
    vi.stubGlobal("fetch", fetchMock);

    await createJobSubmission("token", { input_type: "url", url: "https://jobs.example/1" });
    await fetchDuplicateCandidates("token", "submission/1");
    await submitJobSubmission("token", "submission/1", 3);

    expect(fetchMock.mock.calls[0][0]).toBe("/api/job-submissions");
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/api/job-submissions/submission%2F1/duplicate-candidates",
    );
    expect(JSON.parse(fetchMock.mock.calls[2][1].body)).toEqual({ expected_version: 3 });
  });

  it("sends an explicit administrator decision", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(
      new Response(JSON.stringify({ id: "submission-1" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ));
    vi.stubGlobal("fetch", fetchMock);
    await decideJobSubmission("admin", "submission-1", {
      expected_version: 2,
      action: "link_existing",
      job_id: "00000000-0000-4000-8000-000000000001",
    });
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/admin/job-submissions/submission-1/decision",
    );
  });
});
