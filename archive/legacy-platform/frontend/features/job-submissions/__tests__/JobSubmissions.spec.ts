import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import JobSubmissions from "../JobSubmissions.vue";

const api = vi.hoisted(() => ({
  createJobSubmission: vi.fn(),
  fetchDuplicateCandidates: vi.fn(),
  fetchJobSubmissions: vi.fn(),
  submitJobSubmission: vi.fn(),
  updateJobSubmission: vi.fn(),
}));
vi.mock("../jobSubmissionsApi", () => api);

const draft = {
  id: "submission-1",
  input_type: "jd_text" as const,
  input_preview: "仅有 240 字的截断预览",
  normalized_url: null,
  status: "draft" as const,
  version: 0,
  deduplication_status: "succeeded" as const,
  deduplication_error_code: null,
  promoted_job_id: null,
  created_at: "2026-07-17T00:00:00Z",
  updated_at: "2026-07-17T00:00:00Z",
};

describe("JobSubmissions", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    api.fetchJobSubmissions.mockResolvedValue({ total: 1, submissions: [draft] });
    api.fetchDuplicateCandidates.mockResolvedValue({ candidates: [] });
  });

  it("never seeds a JD replacement with the redacted preview", async () => {
    const wrapper = mount(JobSubmissions, { props: { token: "student-token" } });
    await flushPromises();

    const editButton = wrapper.findAll("button").find((button) => button.text() === "编辑");
    expect(editButton).toBeDefined();
    await editButton!.trigger("click");

    const editor = wrapper.get(".edit-section textarea");
    expect((editor.element as HTMLTextAreaElement).value).toBe("");
    expect(wrapper.text()).toContain("完整的新内容");
  });

  it("emits dirty state while raw manual input is unsaved", async () => {
    const wrapper = mount(JobSubmissions, { props: { token: "student-token" } });
    await flushPromises();

    await wrapper.get(".create-form input.input-field").setValue("https://jobs.example.com/1");
    expect(wrapper.emitted("dirty-change")?.at(-1)).toEqual([true]);
  });

  it("shows why a duplicate is only a candidate and was not auto-merged", async () => {
    api.fetchDuplicateCandidates.mockResolvedValue({
      candidates: [{
        job: {
          id: "job-1",
          company_name: "示例科技",
          title: "后端实习生",
          status: "verified",
          apply_url: "https://jobs.example.com/1",
        },
        score_basis_points: 9300,
        reasons: ["jd_token_overlap"],
        score_components: { jd_token_jaccard: 9300 },
        algorithm_version: "manual-job-dedup-v1",
      }],
    });
    const wrapper = mount(JobSubmissions, { props: { token: "student-token" } });
    await flushPromises();

    const candidatesButton = wrapper.findAll("button").find(
      (button) => button.text() === "候选",
    );
    await candidatesButton!.trigger("click");
    await flushPromises();

    expect(wrapper.text()).toContain("jd_token_overlap");
    expect(wrapper.text()).toContain("候选，不会自动合并");
  });
});
