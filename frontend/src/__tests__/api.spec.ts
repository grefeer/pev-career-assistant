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
});
