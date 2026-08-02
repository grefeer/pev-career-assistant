import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../api";
import JobFeedbackPanel from "../JobFeedbackPanel.vue";
import { fetchMyJobFeedback, mutateJobFeedback } from "../jobFeedbackApi";

vi.mock("../jobFeedbackApi", () => ({
  fetchMyJobFeedback: vi.fn(),
  generateFeedbackKey: vi.fn(() => "stable-generated-key"),
  mutateJobFeedback: vi.fn(),
}));
const fetchMock = vi.mocked(fetchMyJobFeedback);
const mutateMock = vi.mocked(mutateJobFeedback);

describe("JobFeedbackPanel", () => {
  beforeEach(() => {
    fetchMock.mockReset().mockResolvedValue({ feedback: [] });
    mutateMock.mockReset().mockResolvedValue({
      id: "f1", job_id: "job-1", category: "closed", status: "open",
      version: 1, updated_at: "2026-07-17T00:00:00Z",
    });
  });

  it("reuses the same idempotency key when the same request is retried", async () => {
    mutateMock.mockRejectedValueOnce(new Error("network")).mockResolvedValueOnce({
      id: "f1", job_id: "job-1", category: "closed", status: "open",
      version: 1, updated_at: "2026-07-17T00:00:00Z",
    });
    const wrapper = mount(JobFeedbackPanel, { props: { token: "token", jobId: "job-1" } });
    await flushPromises();
    await wrapper.get('[data-test="feedback-submit"]').trigger("click");
    await flushPromises();
    await wrapper.get('[data-test="feedback-submit"]').trigger("click");
    await flushPromises();
    expect(mutateMock).toHaveBeenCalledTimes(2);
    expect(mutateMock.mock.calls[0][3]).toBe(mutateMock.mock.calls[1][3]);
  });

  it("updates and withdraws an existing category using its version", async () => {
    fetchMock.mockResolvedValue({
      feedback: [{
        id: "f1", job_id: "job-1", category: "closed", status: "open",
        note: "old", version: 3, created_at: "2026-07-17T00:00:00Z",
        updated_at: "2026-07-17T00:00:00Z",
      }],
    });
    const wrapper = mount(JobFeedbackPanel, { props: { token: "token", jobId: "job-1" } });
    await flushPromises();
    await wrapper.get('[data-test="feedback-withdraw-f1"]').trigger("click");
    await flushPromises();
    expect(mutateMock.mock.calls[0][2]).toEqual({
      action: "withdraw", category: "closed", expected_version: 3, note: null,
    });
  });

  it("clears stale retries and reloads the current version", async () => {
    mutateMock.mockRejectedValueOnce(
      new ApiError(409, { error_code: "stale_job_feedback" }, "stale"),
    );
    const wrapper = mount(JobFeedbackPanel, { props: { token: "token", jobId: "job-1" } });
    await flushPromises();
    await wrapper.get('[data-test="feedback-submit"]').trigger("click");
    await flushPromises();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
