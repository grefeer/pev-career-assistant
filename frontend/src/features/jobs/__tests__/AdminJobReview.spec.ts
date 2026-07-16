import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../../../api";
import AdminJobReview from "../AdminJobReview.vue";
import {
  decideJob,
  fetchAdminVerifiedJobs,
  fetchJobReviewQueue,
  saveJobCompletion,
} from "../jobsApi";
import type { AdminJobDetail } from "../jobTypes";

vi.mock("../jobsApi", () => ({
  fetchJobReviewQueue: vi.fn(),
  fetchAdminVerifiedJobs: vi.fn(),
  saveJobCompletion: vi.fn(),
  decideJob: vi.fn(),
}));

const pending: AdminJobDetail = {
  id: "job-1",
  company_name: "示例科技",
  title: "来源岗位",
  description_text: "原始 JD",
  locations: ["上海"],
  recruitment_types: ["实习"],
  industries: ["软件"],
  apply_url: "https://example.com/jobs/1",
  referral_code: null,
  deadline_text: null,
  status: "pending_completion",
  gui_eligible: false,
  source_key: "source",
  source_name: "来源",
  updated_at: "2026-07-16T00:00:00Z",
  source_candidate: {
    company_name: "候选科技",
    title: "候选岗位",
    locations: ["杭州"],
    recruitment_types: ["校招"],
    industries: ["互联网"],
    apply_url: "https://candidate.example/apply",
    referral_code: "REF-8",
    deadline_text: "2026-08-01",
  },
  source_changed_since_review: false,
  review_version: 3,
};

function job(overrides: Partial<AdminJobDetail>): AdminJobDetail {
  return { ...pending, ...overrides };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("AdminJobReview", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.stubGlobal("confirm", vi.fn(() => true));
    vi.mocked(fetchJobReviewQueue).mockResolvedValue({ total: 1, jobs: [pending] });
    vi.mocked(fetchAdminVerifiedJobs).mockResolvedValue({ total: 0, jobs: [] });
    vi.mocked(saveJobCompletion).mockResolvedValue(
      job({ status: "pending_review", review_version: 4 }),
    );
    vi.mocked(decideJob).mockResolvedValue(job({ status: "verified", review_version: 5 }));
  });

  it("renders only the eight normalized candidates and saves every completion field with the read version", async () => {
    const leaking = job({
      source_candidate: {
        ...pending.source_candidate,
        raw_fields: "SECRET_RAW",
        payload_hash: "SECRET_HASH",
        token: "SECRET_TOKEN",
        trace: "SECRET_TRACE",
      } as AdminJobDetail["source_candidate"],
    });
    vi.mocked(fetchJobReviewQueue).mockResolvedValue({ total: 1, jobs: [leaking] });
    const wrapper = mount(AdminJobReview, { props: { token: "admin-token" } });
    await flushPromises();

    expect(wrapper.findAll('[data-test^="candidate-"]')).toHaveLength(8);
    expect(wrapper.text()).toContain("候选岗位");
    expect(wrapper.text()).not.toMatch(/SECRET_RAW|SECRET_HASH|SECRET_TOKEN|SECRET_TRACE/);

    await wrapper.get('[data-test="company"]').setValue("新公司");
    await wrapper.get('[data-test="title"]').setValue("后端实习生");
    await wrapper.get('[data-test="description"]').setValue("完整 JD");
    await wrapper.get('[data-test="locations"]').setValue("上海，杭州");
    await wrapper.get('[data-test="recruitment-types"]').setValue("实习，校招");
    await wrapper.get('[data-test="industries"]').setValue("软件，人工智能");
    await wrapper.get('[data-test="apply-url"]').setValue("https://jobs.example/apply");
    await wrapper.get('[data-test="referral-code"]').setValue("NEW-REF");
    await wrapper.get('[data-test="deadline"]').setValue("2026-09-01");

    expect(wrapper.find('[data-test="verify-job"]').exists()).toBe(false);
    await wrapper.get('[data-test="save-completion"]').trigger("click");
    await flushPromises();

    expect(saveJobCompletion).toHaveBeenCalledWith("admin-token", "job-1", {
      expected_version: 3,
      company_name: "新公司",
      title: "后端实习生",
      description_text: "完整 JD",
      locations: ["上海", "杭州"],
      recruitment_types: ["实习", "校招"],
      industries: ["软件", "人工智能"],
      apply_url: "https://jobs.example/apply",
      referral_code: "NEW-REF",
      deadline_text: "2026-09-01",
    });
    expect(wrapper.emitted("dirty-change")?.at(-1)).toEqual([false]);
  });

  it("supports bounded paging and non-terminal status filtering without truncating totals", async () => {
    vi.mocked(fetchJobReviewQueue)
      .mockResolvedValueOnce({ total: 23, jobs: [pending] })
      .mockResolvedValueOnce({ total: 23, jobs: [job({ id: "job-2", title: "第二页" })] })
      .mockResolvedValueOnce({ total: 4, jobs: [] });
    const wrapper = mount(AdminJobReview, { props: { token: "admin-token" } });
    await flushPromises();

    expect(wrapper.text()).toContain("共 23 条");
    await wrapper.get('[data-test="next-page"]').trigger("click");
    await flushPromises();
    expect(fetchJobReviewQueue).toHaveBeenNthCalledWith(2, "admin-token", {
      limit: 10,
      offset: 10,
    });
    expect(wrapper.text()).toContain("第二页");

    await wrapper.get('[data-test="status-filter"]').setValue("rejected");
    await flushPromises();
    expect(fetchJobReviewQueue).toHaveBeenLastCalledWith("admin-token", {
      limit: 10,
      offset: 0,
      reviewStatus: "rejected",
    });
    expect(wrapper.text()).toContain("当前筛选没有待处理职位");
    expect(wrapper.find('option[value="verified"]').exists()).toBe(false);
  });

  it("enforces the state/action matrix, stable reason codes, and manual-channel GUI rules", async () => {
    const wrapper = mount(AdminJobReview, { props: { token: "admin-token" } });
    await flushPromises();
    expect(wrapper.get('[data-test="save-completion"]').exists()).toBe(true);
    expect(wrapper.get('[data-test="reject-job"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="verify-job"]').exists()).toBe(false);

    await wrapper.get('[data-test="reject-reason"]').setValue("invalid_source");
    await wrapper.get('[data-test="reject-job"]').trigger("click");
    await flushPromises();
    expect(decideJob).toHaveBeenCalledWith("admin-token", "job-1", {
      expected_version: 3,
      decision: "reject",
      gui_eligible: false,
      reason_code: "invalid_source",
    });

    vi.mocked(fetchJobReviewQueue).mockResolvedValue({
      total: 1,
      jobs: [job({ status: "pending_review", apply_url: "mailto:jobs@example.com" })],
    });
    await wrapper.get('[data-test="refresh-queue"]').trigger("click");
    await flushPromises();
    expect(wrapper.get('input[value="yes"]').attributes("disabled")).toBeDefined();
    await wrapper.get('input[value="no"]').setValue();
    await wrapper.get('[data-test="verify-job"]').trigger("click");
    await flushPromises();
    expect(decideJob).toHaveBeenLastCalledWith("admin-token", "job-1", {
      expected_version: 3,
      decision: "verify",
      gui_eligible: false,
      reason_code: null,
    });

    vi.mocked(fetchJobReviewQueue).mockResolvedValue({
      total: 1,
      jobs: [job({ status: "rejected" })],
    });
    await wrapper.get('[data-test="review-queue-tab"]').trigger("click");
    await flushPromises();
    expect(wrapper.get('[data-test="save-completion"]').exists()).toBe(true);
    expect(wrapper.find('[data-test="verify-job"]').exists()).toBe(false);
    expect(wrapper.find('[data-test="reject-job"]').exists()).toBe(false);
  });

  it("loads the verified lifecycle separately, reloads after verify, and expires with its current version", async () => {
    const verified = job({ status: "verified", review_version: 9, id: "verified-1" });
    vi.mocked(fetchAdminVerifiedJobs)
      .mockResolvedValueOnce({ total: 1, jobs: [verified] })
      .mockResolvedValueOnce({ total: 0, jobs: [] });
    vi.mocked(decideJob).mockResolvedValue(job({ status: "expired", review_version: 10 }));
    const wrapper = mount(AdminJobReview, { props: { token: "admin-token" } });
    await flushPromises();
    await wrapper.get('[data-test="verified-tab"]').trigger("click");
    await flushPromises();
    expect(fetchAdminVerifiedJobs).toHaveBeenCalledWith("admin-token", { limit: 10, offset: 0 });
    await wrapper.get('[data-test="expire-reason"]').setValue("closed_on_official_site");
    await wrapper.get('[data-test="expire-job"]').trigger("click");
    await flushPromises();
    expect(decideJob).toHaveBeenCalledWith("admin-token", "verified-1", {
      expected_version: 9,
      decision: "expire",
      gui_eligible: false,
      reason_code: "closed_on_official_site",
    });
    expect(fetchAdminVerifiedJobs).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).toContain("职位已标记失效");
  });

  it("reloads the non-terminal queue after verification", async () => {
    vi.mocked(fetchJobReviewQueue).mockResolvedValue({
      total: 1,
      jobs: [job({ status: "pending_review" })],
    });
    const wrapper = mount(AdminJobReview, { props: { token: "admin-token" } });
    await flushPromises();
    await wrapper.get('input[value="no"]').setValue();
    await wrapper.get('[data-test="verify-job"]').trigger("click");
    await flushPromises();
    expect(fetchJobReviewQueue).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).toContain("职位已核验并发布");
  });

  it("reloads the verified lifecycle on stale expiry and preserves an explicit reload failure", async () => {
    const verified = job({ status: "verified", review_version: 11, id: "verified-stale" });
    vi.mocked(fetchAdminVerifiedJobs)
      .mockResolvedValueOnce({ total: 1, jobs: [verified] })
      .mockRejectedValueOnce(new Error("reload unavailable"));
    vi.mocked(decideJob).mockRejectedValue(
      new ApiError(409, { error_code: "stale_job_review" }, "stale_job_review"),
    );
    const wrapper = mount(AdminJobReview, { props: { token: "admin-token" } });
    await flushPromises();
    await wrapper.get('[data-test="verified-tab"]').trigger("click");
    await flushPromises();
    await wrapper.get('[data-test="expire-reason"]').setValue("deadline_passed");
    await wrapper.get('[data-test="expire-job"]').trigger("click");
    await flushPromises();
    expect(fetchAdminVerifiedJobs).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).toContain("职位已被其他审核人更新，但重新加载失败");
  });

  it("shows loading and list errors without pretending the queue is empty", async () => {
    const response = deferred<{ total: number; jobs: AdminJobDetail[] }>();
    vi.mocked(fetchJobReviewQueue).mockReturnValue(response.promise);
    const wrapper = mount(AdminJobReview, { props: { token: "admin-token" } });
    await wrapper.vm.$nextTick();
    expect(wrapper.text()).toContain("正在读取职位");
    response.reject(new ApiError(403, null, "forbidden"));
    await flushPromises();
    expect(wrapper.text()).toContain("当前账号没有职位审核权限");
  });

  it("guards dirty selection, prevents duplicate submits, and ignores a late save for another selection", async () => {
    const second = job({ id: "job-2", title: "第二岗位", review_version: 8 });
    vi.mocked(fetchJobReviewQueue).mockResolvedValue({ total: 2, jobs: [pending, second] });
    const saveResult = deferred<AdminJobDetail>();
    vi.mocked(saveJobCompletion).mockReturnValue(saveResult.promise);
    const wrapper = mount(AdminJobReview, { props: { token: "admin-token" } });
    await flushPromises();

    await wrapper.get('[data-test="title"]').setValue("未保存标题");
    vi.mocked(confirm).mockReturnValueOnce(false);
    await wrapper.get('[data-test="queue-job-job-2"]').trigger("click");
    expect(wrapper.get('[data-test="title"]').element).toHaveProperty("value", "未保存标题");
    vi.mocked(confirm).mockReturnValueOnce(true);
    await wrapper.get('[data-test="queue-job-job-2"]').trigger("click");
    expect(wrapper.get('[data-test="title"]').element).toHaveProperty("value", "第二岗位");

    await wrapper.get('[data-test="save-completion"]').trigger("click");
    await wrapper.get('[data-test="save-completion"]').trigger("click");
    expect(saveJobCompletion).toHaveBeenCalledTimes(1);
    await wrapper.get('[data-test="queue-job-job-1"]').trigger("click");
    saveResult.resolve(job({ id: "job-2", title: "迟到响应", review_version: 9 }));
    await flushPromises();
    expect(wrapper.get('[data-test="title"]').element).toHaveProperty("value", "来源岗位");
  });

  it("does not let a late verification reload switch a newer selection", async () => {
    const first = job({ status: "pending_review" });
    const second = job({ id: "job-2", title: "第二岗位", status: "pending_review", review_version: 8 });
    vi.mocked(fetchJobReviewQueue).mockResolvedValue({ total: 2, jobs: [first, second] });
    const decision = deferred<AdminJobDetail>();
    vi.mocked(decideJob).mockReturnValue(decision.promise);
    const wrapper = mount(AdminJobReview, { props: { token: "admin-token" } });
    await flushPromises();
    await wrapper.get('input[value="no"]').setValue();
    await wrapper.get('[data-test="verify-job"]').trigger("click");
    await wrapper.get('[data-test="queue-job-job-2"]').trigger("click");
    decision.resolve(job({ id: "job-1", status: "verified", review_version: 4 }));
    await flushPromises();
    expect(wrapper.get('[data-test="title"]').element).toHaveProperty("value", "第二岗位");
  });

  it("guards explicit refresh while the current draft is dirty", async () => {
    const wrapper = mount(AdminJobReview, { props: { token: "admin-token" } });
    await flushPromises();
    await wrapper.get('[data-test="title"]').setValue("未保存标题");
    vi.mocked(confirm).mockReturnValueOnce(false);
    await wrapper.get('[data-test="refresh-queue"]').trigger("click");
    await flushPromises();
    expect(fetchJobReviewQueue).toHaveBeenCalledTimes(1);
    expect(wrapper.get('[data-test="title"]').element).toHaveProperty("value", "未保存标题");
  });

  it.each([
    [401, null, "登录状态已失效，请重新登录。"],
    [403, null, "当前账号没有职位审核权限。"],
    [422, { error_code: "incomplete_job" }, "职位信息未通过校验，请检查必填字段。"],
    [409, { error_code: "invalid_job_transition" }, "职位状态已变化，当前操作不再允许。"],
    [500, null, "职位审核操作失败，请稍后重试。"],
  ])("handles %s without treating it as a stale review", async (status, detail, expected) => {
    vi.mocked(saveJobCompletion).mockRejectedValue(new ApiError(status, detail, "server message"));
    const wrapper = mount(AdminJobReview, { props: { token: "admin-token" } });
    await flushPromises();
    await wrapper.get('[data-test="save-completion"]').trigger("click");
    await flushPromises();
    expect(fetchJobReviewQueue).toHaveBeenCalledTimes(1);
    expect(wrapper.text()).toContain(expected);
  });

  it("auto-reloads only exact stale errors and reports stale reload failures explicitly", async () => {
    vi.mocked(saveJobCompletion).mockRejectedValue(
      new ApiError(409, { error_code: "stale_job_review" }, "stale_job_review"),
    );
    vi.mocked(fetchJobReviewQueue)
      .mockResolvedValueOnce({ total: 1, jobs: [pending] })
      .mockRejectedValueOnce(new Error("network down"));
    const wrapper = mount(AdminJobReview, { props: { token: "admin-token" } });
    await flushPromises();
    await wrapper.get('[data-test="save-completion"]').trigger("click");
    await flushPromises();
    expect(fetchJobReviewQueue).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).toContain("职位已被其他审核人更新，但重新加载失败，请手动刷新后再操作。");
  });

  it("warns when the source candidate changed after review", async () => {
    vi.mocked(fetchJobReviewQueue).mockResolvedValue({
      total: 1,
      jobs: [job({ source_changed_since_review: true })],
    });
    const wrapper = mount(AdminJobReview, { props: { token: "admin-token" } });
    await flushPromises();
    expect(wrapper.text()).toContain("来源数据已变化，请对照候选值重新审核");
  });
});
