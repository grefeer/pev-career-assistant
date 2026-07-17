import { describe, it, expect, vi, beforeEach } from "vitest";
import { mount, flushPromises } from "@vue/test-utils";
import ProfileWorkspace from "../ProfileWorkspace.vue";
import * as profileApi from "../profileApi";
import { ApiError } from "../../../api";

vi.mock("../profileApi");

const mockProfile = {
  id: "p1",
  version: 0,
  evidence: [
    {
      id: "evidence-1",
      resume_import_id: "import-1",
      field_path: "skills",
      candidate_value: ["Python"],
      evidence_excerpt: "Python",
      confidence: 90,
      status: "pending" as const,
      diff_action: "add" as const,
    },
  ],
  local_sensitive_references: {},
  latest_version: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(profileApi.fetchProfile).mockResolvedValue({
    ...mockProfile,
    version: 0,
    evidence: mockProfile.evidence.map((evidence) => ({ ...evidence })),
  });
  vi.mocked(profileApi.fetchResumeAssets).mockResolvedValue({ assets: [] });
  vi.mocked(profileApi.fetchProfileVersions).mockResolvedValue({ versions: [] });
  vi.mocked(profileApi.applyEvidenceDecisions).mockResolvedValue({ version: 1 });
  vi.mocked(profileApi.createProfileVersion).mockResolvedValue({
    id: "v1",
    version_number: 1,
    aggregate_version: 1,
    facts_snapshot: {},
    evidence_refs: {},
    local_sensitive_references: {},
    created_at: "2026-07-17T00:00:00Z",
  });
});

describe("ProfileWorkspace", () => {
  it("requires persisted decisions for every candidate before creating a version", async () => {
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    const createBtn = wrapper.get('[data-test="create-version"]');
    expect(createBtn.attributes("disabled")).toBeDefined();
    await wrapper.get('[data-test="decision-confirm-evidence-1"]').trigger("click");
    expect(wrapper.get('[data-test="create-version"]').attributes("disabled")).toBeDefined();

    vi.mocked(profileApi.fetchProfile).mockResolvedValue({
      ...mockProfile,
      version: 1,
      evidence: [{ ...mockProfile.evidence[0], status: "confirmed" }],
    });
    await wrapper.get('[data-test="save-decisions"]').trigger("click");
    await flushPromises();
    expect(wrapper.get('[data-test="create-version"]').attributes("disabled")).toBeUndefined();
  });

  it("requires and submits a value for a correction", async () => {
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();

    await wrapper.get('[data-test="decision-correct-evidence-1"]').trigger("click");
    expect(wrapper.get('[data-test="save-decisions"]').attributes("disabled")).toBeDefined();
    await wrapper.get('[data-test="correction-evidence-1"]').setValue('"TypeScript"');
    expect(wrapper.get('[data-test="save-decisions"]').attributes("disabled")).toBeUndefined();
    await wrapper.get('[data-test="save-decisions"]').trigger("click");

    expect(profileApi.applyEvidenceDecisions).toHaveBeenCalledWith("token", 0, [
      {
        evidence_id: "evidence-1",
        action: "correct",
        corrected_value: "TypeScript",
      },
    ]);
  });

  it("reloads on stale profile version instead of overwriting", async () => {
    vi.mocked(profileApi.applyEvidenceDecisions).mockRejectedValue(
      new ApiError(409, { code: "stale_profile_version" }, "stale_profile_version"),
    );
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    await wrapper.get('[data-test="decision-confirm-evidence-1"]').trigger("click");
    await wrapper.get('[data-test="save-decisions"]').trigger("click");
    await flushPromises();
    // fetchProfile is called on mount, and again after stale error reload
    expect(profileApi.fetchProfile.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(wrapper.text()).toContain("档案已被其他操作更新，请重新检查差异。");
  });
});
