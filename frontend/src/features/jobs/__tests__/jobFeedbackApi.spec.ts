import { beforeEach, describe, expect, it, vi } from "vitest";

import { request } from "../../../api";
import {
  decideJobFeedback,
  fetchAdminJobFeedback,
  fetchMyJobFeedback,
  mutateJobFeedback,
} from "../jobFeedbackApi";

vi.mock("../../../api", () => ({ request: vi.fn() }));
const requestMock = vi.mocked(request);

describe("jobFeedbackApi", () => {
  beforeEach(() => requestMock.mockReset());

  it("uses job-scoped student routes and caller-supplied retry key", async () => {
    requestMock.mockResolvedValue({});
    await mutateJobFeedback(
      "token", "job/1",
      { action: "upsert", category: "closed", expected_version: null, note: null },
      "stable-key-000001",
    );
    expect(requestMock).toHaveBeenCalledWith(
      "/jobs/job%2F1/feedback",
      expect.objectContaining({
        method: "POST",
        headers: { "Idempotency-Key": "stable-key-000001" },
      }),
      "token",
    );
    await fetchMyJobFeedback("token", "job/1");
    expect(requestMock).toHaveBeenLastCalledWith("/jobs/job%2F1/feedback", {}, "token");
  });

  it("uses identity-free admin queue and decision routes", async () => {
    requestMock.mockResolvedValue({});
    await fetchAdminJobFeedback("admin", { status: "open", category: "closed" });
    expect(requestMock.mock.calls[0][0]).toContain("/admin/job-feedback?");
    await decideJobFeedback(
      "admin", "feedback/1", { decision: "resolve", expected_version: 1 },
      "admin-key-000001",
    );
    expect(requestMock).toHaveBeenLastCalledWith(
      "/admin/job-feedback/feedback%2F1/decision",
      expect.objectContaining({ headers: { "Idempotency-Key": "admin-key-000001" } }),
      "admin",
    );
  });
});
