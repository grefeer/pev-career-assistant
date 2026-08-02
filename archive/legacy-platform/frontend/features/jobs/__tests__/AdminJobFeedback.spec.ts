import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../api";
import AdminJobFeedback from "../AdminJobFeedback.vue";
import { decideJobFeedback, fetchAdminJobFeedback } from "../jobFeedbackApi";

vi.mock("../jobFeedbackApi", () => ({
  decideJobFeedback: vi.fn(),
  fetchAdminJobFeedback: vi.fn(),
  generateFeedbackKey: vi.fn(() => "stable-admin-key"),
}));

describe("AdminJobFeedback", () => {
  beforeEach(() => {
    vi.mocked(fetchAdminJobFeedback).mockReset().mockResolvedValue({
      total: 1,
      feedback: [{
        id: "f1", job_id: "j1", company_name: "Company", title: "Role",
        job_status: "verified", job_review_version: 0, category: "closed",
        status: "open", note: "closed", version: 1,
        created_at: "2026-07-17T00:00:00Z", updated_at: "2026-07-17T00:00:00Z",
      }],
      aggregates: [{
        job_id: "j1", company_name: "Company", title: "Role", category: "closed",
        open_count: 1, accepted_count: 0, total_count: 1,
        latest_updated_at: "2026-07-17T00:00:00Z",
      }],
    });
    vi.mocked(decideJobFeedback).mockReset().mockResolvedValue({
      id: "f1", job_id: "j1", category: "closed", status: "resolved",
      version: 2, updated_at: "2026-07-17T00:00:00Z",
    });
  });

  it("renders aggregates and sends versioned decisions", async () => {
    const wrapper = mount(AdminJobFeedback, { props: { token: "admin" } });
    await flushPromises();
    expect(wrapper.text()).toContain("1 条反馈");
    await wrapper.get('[data-test="resolve-f1"]').trigger("click");
    await flushPromises();
    expect(vi.mocked(decideJobFeedback).mock.calls[0][2]).toEqual({
      decision: "resolve", expected_version: 1,
    });
    expect(wrapper.text()).toContain("失效请前往职位审核");
  });

  it("supports filters and reloads after a stale decision", async () => {
    vi.mocked(decideJobFeedback).mockRejectedValueOnce(
      new ApiError(409, { error_code: "stale_job_feedback" }, "stale"),
    );
    const wrapper = mount(AdminJobFeedback, { props: { token: "admin" } });
    await flushPromises();
    await wrapper.get('[data-test="status-filter"]').setValue("accepted");
    await flushPromises();
    expect(vi.mocked(fetchAdminJobFeedback)).toHaveBeenLastCalledWith(
      "admin", expect.objectContaining({ status: "accepted" }),
    );
    await wrapper.get('[data-test="resolve-f1"]').trigger("click");
    await flushPromises();
    expect(vi.mocked(fetchAdminJobFeedback).mock.calls.length).toBeGreaterThan(2);
  });
});
