import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, request } from "../api";

describe("request", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("preserves structured API error details and a user-facing message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(
          JSON.stringify({
            detail: {
              error_code: "stale_job_review",
              message: "职位已更新，请重新加载。",
            },
          }),
          { status: 409, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );

    const error = await request("/jobs", {}, "student-token").catch((caught) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 409,
      detail: {
        error_code: "stale_job_review",
        message: "职位已更新，请重新加载。",
      },
      message: "职位已更新，请重新加载。",
    });
  });

  it("keeps a non-empty fallback message for a non-JSON error response", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("upstream unavailable", {
          status: 502,
          headers: { "Content-Type": "text/plain" },
        }),
      ),
    );

    const error = await request("/jobs").catch((caught) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 502,
      detail: null,
      message: "请求失败：502",
    });
  });

  it("uses detail.code when the API omits a display message", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: { code: "stale_job_submission" } }), {
          status: 409,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    const error = await request("/job-submissions/1").catch((caught) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toBe("stale_job_submission");
  });

  it.each([
    { detail: { error_code: "" } },
    { detail: { error_code: "", message: "" } },
    { detail: [] },
    { detail: { error_code: null, message: 42 } },
    { detail: "   " },
  ])("keeps the HTTP fallback for empty or malformed detail %#", async (body) => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify(body), {
          status: 500,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    const error = await request("/jobs").catch((caught) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect(error.message).toBe("请求失败：500");
  });
});
