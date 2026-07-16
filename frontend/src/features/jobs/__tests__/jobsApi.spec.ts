import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchVerifiedJob, fetchVerifiedJobs } from "../jobsApi";

describe("verified jobs API", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("queries the single public jobs route with pagination and filters", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ total: 0, jobs: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
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
