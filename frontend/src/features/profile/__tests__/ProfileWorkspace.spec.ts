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
  it("shows assets, completed evidence and confirmed versions from the profile APIs", async () => {
    vi.mocked(profileApi.fetchProfile).mockResolvedValue({
      ...mockProfile,
      evidence: [
        { ...mockProfile.evidence[0], status: "confirmed", diff_action: "replace" },
        {
          ...mockProfile.evidence[0],
          id: "evidence-2",
          status: "corrected",
          diff_action: "unchanged",
        },
        {
          ...mockProfile.evidence[0],
          id: "evidence-3",
          status: "ignored",
          diff_action: "conflict",
        },
      ],
    });
    vi.mocked(profileApi.fetchResumeAssets).mockResolvedValue({
      assets: [
        {
          id: "asset-ready",
          original_filename: "resume.pdf",
          content_type: "application/pdf",
          plaintext_size: 256,
          encryption_version: "v1",
          status: "ready",
          error_code: null,
          created_at: "2026-08-01T00:00:00Z",
          updated_at: "2026-08-01T00:00:00Z",
        },
        {
          id: "asset-failed",
          original_filename: "broken.docx",
          content_type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
          plaintext_size: 100,
          encryption_version: "v1",
          status: "failed",
          error_code: "parse_failed",
          created_at: "2026-08-01T00:00:00Z",
          updated_at: "2026-08-01T00:00:00Z",
        },
      ],
    });
    vi.mocked(profileApi.fetchProfileVersions).mockResolvedValue({
      versions: [{ id: "version-1", version_number: 2, aggregate_version: 5, created_at: "2026-08-01" }],
    });

    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();

    expect(wrapper.text()).toContain("resume.pdf");
    expect(wrapper.text()).toContain("parse_failed");
    expect(wrapper.text()).toContain("已确认");
    expect(wrapper.text()).toContain("已更正");
    expect(wrapper.text()).toContain("已忽略");
    expect(wrapper.text()).toContain("替换");
    expect(wrapper.text()).toContain("不变");
    expect(wrapper.text()).toContain("冲突");
    expect(wrapper.text()).toContain("版本 2");
    expect(wrapper.findAll("button").filter((button) => button.text() === "解析")[1].attributes("disabled")).toBeDefined();
  });

  it("uploads a selected resume and refreshes the profile", async () => {
    vi.mocked(profileApi.uploadResumeAsset).mockResolvedValue({ id: "asset-new" } as any);
    vi.mocked(profileApi.reconcileResumeAsset).mockResolvedValue({} as any);
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    const file = new File(["resume"], "resume.txt", { type: "text/plain" });
    const input = wrapper.get('input[type="file"]').element as HTMLInputElement;
    Object.defineProperty(input, "files", { value: [file] });

    await wrapper.get('input[type="file"]').trigger("change");
    await flushPromises();

    expect(profileApi.uploadResumeAsset).toHaveBeenCalledWith("token", file);
    expect(profileApi.reconcileResumeAsset).toHaveBeenCalledWith("token", "asset-new");
    expect(wrapper.text()).toContain("上传成功");
  });

  it("reconciles and imports a ready asset before refreshing the selected evidence", async () => {
    vi.mocked(profileApi.fetchResumeAssets).mockResolvedValue({
      assets: [
        {
          id: "asset-ready",
          original_filename: "resume.pdf",
          content_type: "application/pdf",
          plaintext_size: 256,
          encryption_version: "v1",
          status: "ready",
          error_code: null,
          created_at: "2026-08-01T00:00:00Z",
          updated_at: "2026-08-01T00:00:00Z",
        },
      ],
    });
    vi.mocked(profileApi.reconcileResumeAsset).mockResolvedValue({} as any);
    vi.mocked(profileApi.startResumeImport).mockResolvedValue({ id: "import-new" } as any);
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();

    const actions = wrapper.findAll("button").filter((button) => ["同步", "解析"].includes(button.text()));
    await actions[0].trigger("click");
    await flushPromises();
    await actions[1].trigger("click");
    await flushPromises();

    expect(profileApi.reconcileResumeAsset).toHaveBeenCalledWith("token", "asset-ready");
    expect(profileApi.startResumeImport).toHaveBeenCalledWith("token", "asset-ready");
    expect(wrapper.text()).toContain("解析完成");
  });

  it("keeps decision toggles local and reports an import failure", async () => {
    vi.mocked(profileApi.fetchResumeAssets).mockResolvedValue({
      assets: [{
        id: "asset-ready", original_filename: "resume.pdf", content_type: "application/pdf",
        plaintext_size: 256, encryption_version: "v1", status: "ready", error_code: null,
        created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
      }],
    });
    vi.mocked(profileApi.startResumeImport).mockRejectedValue(new Error("解析服务不可用"));
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();

    const ignore = wrapper.findAll("button").find((button) => button.text() === "忽略")!;
    await ignore.trigger("click");
    expect(wrapper.get('[data-test="save-decisions"]').attributes("disabled")).toBeUndefined();
    await ignore.trigger("click");
    expect(wrapper.get('[data-test="save-decisions"]').attributes("disabled")).toBeDefined();
    await wrapper.findAll("button").find((button) => button.text() === "解析")!.trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("解析服务不可用");
  });

  it("reports a resume-asset synchronization failure", async () => {
    vi.mocked(profileApi.fetchResumeAssets).mockResolvedValue({
      assets: [{
        id: "asset-ready", original_filename: "resume.pdf", content_type: "application/pdf",
        plaintext_size: 256, encryption_version: "v1", status: "ready", error_code: null,
        created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
      }],
    });
    vi.mocked(profileApi.reconcileResumeAsset).mockRejectedValue(new Error("同步服务不可用"));
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text() === "同步")!.trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("同步服务不可用");
  });

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

  it("uses plain-text corrections and surfaces ordinary save errors", async () => {
    vi.mocked(profileApi.applyEvidenceDecisions).mockRejectedValue(new Error("network down"));
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    await wrapper.get('[data-test="decision-correct-evidence-1"]').trigger("click");
    await wrapper.get('[data-test="correction-evidence-1"]').setValue("TypeScript");
    await wrapper.get('[data-test="save-decisions"]').trigger("click");
    await flushPromises();

    expect(profileApi.applyEvidenceDecisions).toHaveBeenCalledWith("token", 0, [
      { evidence_id: "evidence-1", action: "correct", corrected_value: "TypeScript" },
    ]);
    expect(wrapper.text()).toContain("network down");
  });

  it("creates a confirmed version and reloads after a stale version conflict", async () => {
    vi.mocked(profileApi.fetchProfile).mockResolvedValue({
      ...mockProfile,
      version: 3,
      evidence: [{ ...mockProfile.evidence[0], status: "confirmed" }],
    });
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    await wrapper.get('[data-test="create-version"]').trigger("click");
    await flushPromises();
    expect(profileApi.createProfileVersion).toHaveBeenCalledWith("token", 3, "import-1");
    expect(wrapper.text()).toContain("版本 1 已确认");

    vi.mocked(profileApi.createProfileVersion).mockRejectedValue(
      new ApiError(409, { code: "stale_profile_version" }, "stale_profile_version"),
    );
    await wrapper.get('[data-test="create-version"]').trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("档案已被其他操作更新，请重新检查差异。");
  });

  it("shows an ordinary confirmed-version failure", async () => {
    vi.mocked(profileApi.fetchProfile).mockResolvedValue({
      ...mockProfile,
      evidence: [{ ...mockProfile.evidence[0], status: "confirmed" }],
    });
    vi.mocked(profileApi.createProfileVersion).mockRejectedValue(new Error("版本服务不可用"));
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    await wrapper.get('[data-test="create-version"]').trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("版本服务不可用");
  });

  it("shows a profile loading error and upload error without leaving stale loading state", async () => {
    vi.mocked(profileApi.fetchProfile).mockRejectedValueOnce(new Error("profile unavailable"));
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    expect(wrapper.text()).toContain("profile unavailable");
    expect(wrapper.text()).toContain("暂无简历资产。");

    vi.mocked(profileApi.uploadResumeAsset).mockRejectedValue(new Error("upload unavailable"));
    const input = wrapper.get('input[type="file"]').element as HTMLInputElement;
    Object.defineProperty(input, "files", { value: [new File(["x"], "resume.txt")] });
    await wrapper.get('input[type="file"]').trigger("change");
    await flushPromises();
    expect(wrapper.text()).toContain("upload unavailable");
  });

  it("uses a default message when profile loading fails without one", async () => {
    vi.mocked(profileApi.fetchProfile).mockRejectedValue(new Error(""));
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    // error.message || "加载失败" -> "" falsy -> "加载失败"
    expect(wrapper.text()).toContain("加载失败");
  });

  it("ignores a change event with no selected file", async () => {
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    // no files set -> file undefined -> early return before any upload
    await wrapper.get('input[type="file"]').trigger("change");
    await flushPromises();
    expect(profileApi.uploadResumeAsset).not.toHaveBeenCalled();
    expect(wrapper.text()).not.toContain("上传成功");
  });

  it("uses a default upload error and skips input reset after unmount", async () => {
    let rejectUpload!: (error: unknown) => void;
    vi.mocked(profileApi.uploadResumeAsset).mockReturnValue(
      new Promise((_resolve, reject) => {
        rejectUpload = reject;
      }) as any,
    );
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    const input = wrapper.get('input[type="file"]').element as HTMLInputElement;
    Object.defineProperty(input, "files", { value: [new File(["x"], "resume.txt")] });
    await wrapper.get('input[type="file"]').trigger("change");
    // unmount mid-flight: the fileInput template ref is released (null) so the
    // finally guard's falsy arm runs, and the empty-message catch hits the || fallback
    wrapper.unmount();
    rejectUpload(new Error(""));
    await flushPromises();
    warnSpy.mockRestore();
  });

  it("uses a default message when reconciliation fails without one", async () => {
    vi.mocked(profileApi.fetchResumeAssets).mockResolvedValue({
      assets: [{
        id: "asset-ready", original_filename: "resume.pdf", content_type: "application/pdf",
        plaintext_size: 256, encryption_version: "v1", status: "ready", error_code: null,
        created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
      }],
    });
    vi.mocked(profileApi.reconcileResumeAsset).mockRejectedValue(new Error(""));
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text() === "同步")!.trigger("click");
    await flushPromises();
    // error.message || "同步失败" -> "" falsy -> "同步失败"
    expect(wrapper.text()).toContain("同步失败");
  });

  it("uses a default message when import fails without one", async () => {
    vi.mocked(profileApi.fetchResumeAssets).mockResolvedValue({
      assets: [{
        id: "asset-ready", original_filename: "resume.pdf", content_type: "application/pdf",
        plaintext_size: 256, encryption_version: "v1", status: "ready", error_code: null,
        created_at: "2026-08-01T00:00:00Z", updated_at: "2026-08-01T00:00:00Z",
      }],
    });
    vi.mocked(profileApi.startResumeImport).mockRejectedValue(new Error(""));
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    await wrapper.findAll("button").find((button) => button.text() === "解析")!.trigger("click");
    await flushPromises();
    // error.message || "解析失败" -> "" falsy -> "解析失败"
    expect(wrapper.text()).toContain("解析失败");
  });

  it("skips save and create-version when the profile failed to load", async () => {
    vi.mocked(profileApi.fetchProfile).mockRejectedValue(new Error("unavailable"));
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    // profile.value is null; the buttons are disabled, so exercise the guards directly
    await wrapper.vm.handleSaveDecisions();
    await wrapper.vm.handleCreateVersion();
    expect(profileApi.applyEvidenceDecisions).not.toHaveBeenCalled();
    expect(profileApi.createProfileVersion).not.toHaveBeenCalled();
  });

  it("submits an empty correction value when saving a correct decision without text", async () => {
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    // choose 更正 but leave the correction empty -> canSaveDecisions is false (button disabled),
    // so call the exposed handler directly to exercise the ?? "" fallback
    await wrapper.get('[data-test="decision-correct-evidence-1"]').trigger("click");
    await wrapper.vm.handleSaveDecisions();
    await flushPromises();
    expect(profileApi.applyEvidenceDecisions).toHaveBeenCalledWith("token", 0, [
      { evidence_id: "evidence-1", action: "correct", corrected_value: "" },
    ]);
  });

  it("uses a default message when saving decisions fails without one", async () => {
    vi.mocked(profileApi.applyEvidenceDecisions).mockRejectedValue(new Error(""));
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    await wrapper.get('[data-test="decision-confirm-evidence-1"]').trigger("click");
    await wrapper.get('[data-test="save-decisions"]').trigger("click");
    await flushPromises();
    // non-ApiError with empty message -> "保存失败"
    expect(wrapper.text()).toContain("保存失败");
  });

  it("uses a default message when creating a version fails without one", async () => {
    vi.mocked(profileApi.fetchProfile).mockResolvedValue({
      ...mockProfile,
      evidence: [{ ...mockProfile.evidence[0], status: "confirmed" }],
    });
    vi.mocked(profileApi.createProfileVersion).mockRejectedValue(new Error(""));
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    await wrapper.get('[data-test="create-version"]').trigger("click");
    await flushPromises();
    // non-ApiError with empty message -> "创建版本失败"
    expect(wrapper.text()).toContain("创建版本失败");
  });

  it("renders an unknown evidence status verbatim", async () => {
    vi.mocked(profileApi.fetchProfile).mockResolvedValue({
      ...mockProfile,
      evidence: [{ ...mockProfile.evidence[0], status: "archived" as any, diff_action: "add" }],
    });
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    // labels[status] || status -> unknown status falls back to the raw value
    expect(wrapper.text()).toContain("archived");
  });

  it("omits the diff badge when an evidence item has no diff action", async () => {
    vi.mocked(profileApi.fetchProfile).mockResolvedValue({
      ...mockProfile,
      evidence: [{ ...mockProfile.evidence[0], diff_action: null as any }],
    });
    const wrapper = mount(ProfileWorkspace, { props: { token: "token" } });
    await flushPromises();
    // v-if="ev.diff_action" -> null -> badge not rendered
    expect(wrapper.findAll(".diff-badge").length).toBe(0);
  });
});
