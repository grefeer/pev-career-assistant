import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AdminJobSubmissions from "../AdminJobSubmissions.vue";

const api = vi.hoisted(() => ({
  decideJobSubmission: vi.fn(),
  fetchAdminDuplicateCandidates: vi.fn(),
  fetchAdminJobSubmissions: vi.fn(),
  fetchDuplicateCandidates: vi.fn(),
}));
vi.mock("../jobSubmissionsApi", () => api);

const submitted = {
  id: "submission-1",
  input_type: "jd_text" as const,
  input_preview: "示例科技招聘后端实习生",
  normalized_url: null,
  status: "submitted" as const,
  version: 2,
  deduplication_status: "succeeded" as const,
  deduplication_error_code: null,
  promoted_job_id: null,
  content_sha256: "a".repeat(64),
  created_at: "2026-07-17T00:00:00Z",
  updated_at: "2026-07-17T00:00:00Z",
};

describe("AdminJobSubmissions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchAdminJobSubmissions.mockResolvedValue({ total: 1, submissions: [submitted] });
    api.fetchAdminDuplicateCandidates.mockResolvedValue({ candidates: [] });
    api.fetchDuplicateCandidates.mockResolvedValue({ candidates: [] });
    api.decideJobSubmission.mockResolvedValue({ ...submitted, status: "promoted", version: 3 });
  });

  it("loads candidates through the administrator-only endpoint", async () => {
    api.fetchAdminDuplicateCandidates.mockResolvedValue({
      candidates: [{
        job: {
          id: "job-1",
          company_name: "示例科技",
          title: "后端实习生",
          status: "pending_completion",
          apply_url: "https://jobs.example.com/1",
        },
        score_basis_points: 9300,
        reasons: ["jd_token_overlap"],
        score_components: { jd_token_jaccard: 9300 },
        algorithm_version: "manual-job-dedup-v1",
      }],
    });
    const wrapper = mount(AdminJobSubmissions, { props: { token: "admin-token" } });
    await flushPromises();

    const reviewButton = wrapper.findAll("button").find((button) => button.text() === "审核");
    expect(reviewButton).toBeDefined();
    await reviewButton!.trigger("click");
    await flushPromises();

    expect(api.fetchAdminDuplicateCandidates).toHaveBeenCalledWith(
      "admin-token",
      "submission-1",
    );
    expect(api.fetchDuplicateCandidates).not.toHaveBeenCalled();
    expect(wrapper.text()).toContain("jd_token_overlap");
  });

  it("keeps expected_version numeric and clears dirty state after a decision", async () => {
    const wrapper = mount(AdminJobSubmissions, { props: { token: "admin-token" } });
    await flushPromises();
    const reviewButton = wrapper.findAll("button").find((button) => button.text() === "审核");
    await reviewButton!.trigger("click");
    await flushPromises();

    const createButton = wrapper.findAll("button").find(
      (button) => button.text() === "创建待补全",
    );
    await createButton!.trigger("click");
    const inputs = wrapper.findAll(".decision-fields input");
    await inputs[0].setValue("示例科技");
    await inputs[1].setValue("后端实习生");
    expect(wrapper.emitted("dirty-change")?.at(-1)).toEqual([true]);

    const confirmButton = wrapper.findAll("button").find((button) => button.text() === "确认");
    await confirmButton!.trigger("click");
    await flushPromises();

    expect(api.decideJobSubmission).toHaveBeenCalledWith("admin-token", "submission-1", {
      expected_version: 2,
      action: "create_pending",
      company_name: "示例科技",
      title: "后端实习生",
    });
    expect(wrapper.emitted("dirty-change")?.at(-1)).toEqual([false]);
  });
});
