import { afterEach, describe, expect, it, vi } from "vitest";

import {
  decideJob,
  fetchAdminVerifiedJobs,
  fetchJobReviewQueue,
  fetchVerifiedJob,
  fetchVerifiedJobs,
  saveJobCompletion,
  syncJobSource,
} from "../jobsApi";

describe("verified jobs API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("queries the single public jobs route with pagination and filters", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(
      new Response(JSON.stringify({ total: 0, jobs: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ));
    vi.stubGlobal("fetch", fetchMock);

    await fetchVerifiedJobs("student-token", {
      limit: 6,
      offset: 12,
      company: "示例 科技",
      recruitmentType: "实习",
      sourceKey: "campus",
    });

    const [url, init] = fetchMock.mock.calls[0];
    const query = new URL(String(url), "https://app.example");
    expect(query.pathname).toBe("/api/jobs");
    expect(Object.fromEntries(query.searchParams)).toEqual({
      limit: "6",
      offset: "12",
      company: "示例 科技",
      recruitment_type: "实习",
      source_key: "campus",
    });
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer student-token");
  });

  it("loads public details from the verified job detail route", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          id: "job/1",
          company_name: "示例科技",
          title: "后端实习生",
          locations: ["上海"],
          recruitment_types: ["实习"],
          industries: ["软件"],
          apply_url: "https://example.com/jobs/1",
          deadline_text: null,
          status: "verified",
          gui_eligible: true,
          source_key: "campus",
          source_name: "校园招聘",
          updated_at: "2026-07-16T00:00:00Z",
          description_text: "负责后端服务开发。",
          referral_code: null,
          verified_at: "2026-07-16T00:00:00Z",
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);

    await fetchVerifiedJob("student-token", "job/1");

    expect(fetchMock.mock.calls[0][0]).toBe("/api/jobs/job%2F1");
  });
});

describe("administrator jobs API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("queries review and verified lifecycle lists with explicit pagination and queue status", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(
      new Response(JSON.stringify({ total: 0, jobs: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ));
    vi.stubGlobal("fetch", fetchMock);

    await fetchJobReviewQueue("admin-token", {
      limit: 10,
      offset: 20,
      reviewStatus: "pending_review",
    });
    await fetchAdminVerifiedJobs("admin-token", { limit: 10, offset: 30 });

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/admin/jobs/review-queue?limit=10&offset=20&review_status=pending_review",
    );
    expect(fetchMock.mock.calls[1][0]).toBe("/api/admin/jobs/verified?limit=10&offset=30");
  });

  it("encodes job ids for completion and decision writes", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(
      new Response(JSON.stringify({ id: "job/1" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ));
    vi.stubGlobal("fetch", fetchMock);
    const completion = {
      expected_version: 2,
      company_name: "公司",
      title: "岗位",
      description_text: "JD",
      locations: ["上海"],
      recruitment_types: ["实习"],
      industries: ["软件"],
      apply_url: "https://example.com",
      referral_code: null,
      deadline_text: null,
    };

    await saveJobCompletion("admin-token", "job/1", completion);
    await decideJob("admin-token", "job/1", {
      expected_version: 3,
      decision: "reject",
      gui_eligible: false,
      reason_code: "invalid_source",
    });

    expect(fetchMock.mock.calls[0][0]).toBe("/api/admin/jobs/job%2F1/completion");
    expect(fetchMock.mock.calls[0][1].method).toBe("PATCH");
    expect(fetchMock.mock.calls[1][0]).toBe("/api/admin/jobs/job%2F1/decision");
    expect(fetchMock.mock.calls[1][1].method).toBe("POST");
  });

  it("posts to a fixed Tencent job source sync route", async () => {
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(
      new Response(JSON.stringify({
        run_id: "run-1",
        source_key: "tencent-intern-referrals",
        status: "succeeded",
        pages_read: 1,
        records_read: 2,
        raw_snapshots_created: 2,
        postings_created: 1,
        postings_updated: 1,
        records_skipped_incomplete: 0,
        started_at: "2026-07-18T00:00:00Z",
        finished_at: "2026-07-18T00:00:01Z",
      }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    ));
    vi.stubGlobal("fetch", fetchMock);

    await syncJobSource("admin-token", "tencent-intern-referrals");

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/api/admin/job-sources/tencent-intern-referrals/sync",
    );
    expect(fetchMock.mock.calls[0][1].method).toBe("POST");
    expect(new Headers(fetchMock.mock.calls[0][1].headers).get("Authorization")).toBe(
      "Bearer admin-token",
    );
  });
});
