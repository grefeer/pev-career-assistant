import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import JobCenter from "../JobCenter.vue";
import { fetchVerifiedJob, fetchVerifiedJobs } from "../jobsApi";
import type { JobDetail, JobListResponse, JobSummary } from "../jobTypes";

vi.mock("../jobsApi", () => ({
  fetchVerifiedJob: vi.fn(),
  fetchVerifiedJobs: vi.fn(),
}));

const job: JobSummary = {
  id: "job-1",
  company_name: "示例科技",
  title: "后端实习生",
  locations: ["上海"],
  recruitment_types: ["实习"],
  industries: ["软件"],
  apply_url: "https://example.com/jobs/1",
  deadline_text: "2026-09-01",
  status: "verified",
  gui_eligible: true,
  source_key: "campus",
  source_name: "校园招聘",
  updated_at: "2026-07-16T00:00:00Z",
};

const detail: JobDetail = {
  ...job,
  description_text: "负责后端服务开发与质量保障。",
  referral_code: "REF-2026",
  verified_at: "2026-07-16T00:00:00Z",
};

const listMock = vi.mocked(fetchVerifiedJobs);
const detailMock = vi.mocked(fetchVerifiedJob);

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((resolvePromise) => {
    resolve = resolvePromise;
  });
  return { promise, resolve };
}

function jobWith(id: string, companyName: string): JobSummary {
  return {
    ...job,
    id,
    company_name: companyName,
    apply_url: `https://example.com/jobs/${id}`,
  };
}

describe("JobCenter", () => {
  beforeEach(() => {
    listMock.mockReset();
    detailMock.mockReset();
    listMock.mockResolvedValue({ total: 1, jobs: [job] });
    detailMock.mockResolvedValue(detail);
  });

  it("shows a loading state before verified jobs resolve", async () => {
    listMock.mockReturnValue(new Promise(() => undefined));

    const wrapper = mount(JobCenter, { props: { token: "student-token" } });

    expect(wrapper.get('[role="status"]').text()).toContain("正在加载职位");
  });

  it("renders verified jobs returned by the API", async () => {
    const wrapper = mount(JobCenter, { props: { token: "student-token" } });
    await flushPromises();

    expect(wrapper.text()).toContain("示例科技");
    expect(wrapper.text()).toContain("后端实习生");
    expect(wrapper.text()).toContain("可使用辅助填写");
    expect(wrapper.get('a[href="https://example.com/jobs/1"]').attributes("rel")).toBe(
      "noopener noreferrer",
    );
  });

  it("renders an empty state", async () => {
    listMock.mockResolvedValue({ total: 0, jobs: [] });

    const wrapper = mount(JobCenter, { props: { token: "student-token" } });
    await flushPromises();

    expect(wrapper.text()).toContain("当前没有符合条件的已核验职位");
  });

  it("renders an API error and retries the current query", async () => {
    listMock.mockRejectedValueOnce(new Error("服务暂时不可用"));

    const wrapper = mount(JobCenter, { props: { token: "student-token" } });
    await flushPromises();

    expect(wrapper.get('[role="alert"]').text()).toContain("服务暂时不可用");
    await wrapper.get('[data-test="retry-jobs"]').trigger("click");
    await flushPromises();
    expect(listMock).toHaveBeenCalledTimes(2);
    expect(wrapper.text()).toContain("示例科技");
  });

  it("applies filters and paginates against the public list endpoint", async () => {
    listMock.mockResolvedValue({ total: 13, jobs: [job] });
    const wrapper = mount(JobCenter, { props: { token: "student-token" } });
    await flushPromises();

    await wrapper.get('[data-test="company-filter"]').setValue("示例科技");
    await wrapper.get('[data-test="type-filter"]').setValue("实习");
    await wrapper.get('[data-test="source-filter"]').setValue("campus");
    await wrapper.get("form").trigger("submit");
    await flushPromises();

    expect(listMock).toHaveBeenLastCalledWith("student-token", {
      limit: 6,
      offset: 0,
      company: "示例科技",
      recruitmentType: "实习",
      sourceKey: "campus",
    });

    await wrapper.get('[data-test="next-page"]').trigger("click");
    await flushPromises();
    expect(listMock).toHaveBeenLastCalledWith("student-token", {
      limit: 6,
      offset: 6,
      company: "示例科技",
      recruitmentType: "实习",
      sourceKey: "campus",
    });
    expect(wrapper.text()).toContain("第 2 / 3 页");
  });

  it("loads and renders the public detail DTO without internal review fields", async () => {
    const wrapper = mount(JobCenter, { props: { token: "student-token" } });
    await flushPromises();

    await wrapper.get('[data-test="show-detail-job-1"]').trigger("click");
    await flushPromises();

    expect(detailMock).toHaveBeenCalledWith("student-token", "job-1");
    expect(wrapper.text()).toContain("负责后端服务开发与质量保障");
    expect(wrapper.text()).toContain("REF-2026");
    expect(wrapper.text()).toContain("2026-07-16");
    expect(wrapper.text()).not.toContain("source_candidate");
    expect(wrapper.text()).not.toContain("review_version");
  });

  it("keeps the latest filter result when list requests resolve out of order", async () => {
    const staleResponse = deferred<JobListResponse>();
    const latestResponse = deferred<JobListResponse>();
    listMock
      .mockReset()
      .mockResolvedValueOnce({ total: 1, jobs: [job] })
      .mockReturnValueOnce(staleResponse.promise)
      .mockReturnValueOnce(latestResponse.promise);

    const wrapper = mount(JobCenter, { props: { token: "student-token" } });
    await flushPromises();

    await wrapper.get('[data-test="company-filter"]').setValue("旧筛选");
    await wrapper.get("form").trigger("submit");
    await wrapper.get('[data-test="company-filter"]').setValue("新筛选");
    await wrapper.get("form").trigger("submit");

    latestResponse.resolve({ total: 1, jobs: [jobWith("latest", "最新公司")] });
    await flushPromises();
    expect(wrapper.text()).toContain("最新公司");

    staleResponse.resolve({ total: 1, jobs: [jobWith("stale", "过期公司")] });
    await flushPromises();
    expect(wrapper.text()).toContain("最新公司");
    expect(wrapper.text()).not.toContain("过期公司");
  });

  it("keeps the latest page when pagination requests resolve out of order", async () => {
    const pageTwoResponse = deferred<JobListResponse>();
    const pageThreeResponse = deferred<JobListResponse>();
    listMock
      .mockReset()
      .mockResolvedValueOnce({ total: 13, jobs: [job] })
      .mockReturnValueOnce(pageTwoResponse.promise)
      .mockReturnValueOnce(pageThreeResponse.promise);

    const wrapper = mount(JobCenter, { props: { token: "student-token" } });
    await flushPromises();

    const nextButton = wrapper.get('[data-test="next-page"]');
    const pageTwoClick = nextButton.trigger("click");
    const pageThreeClick = nextButton.trigger("click");
    await Promise.all([pageTwoClick, pageThreeClick]);

    pageThreeResponse.resolve({ total: 13, jobs: [jobWith("page-3", "第三页公司")] });
    await flushPromises();
    expect(wrapper.text()).toContain("第 3 / 3 页");
    expect(wrapper.text()).toContain("第三页公司");

    pageTwoResponse.resolve({ total: 13, jobs: [jobWith("page-2", "第二页公司")] });
    await flushPromises();
    expect(wrapper.text()).toContain("第 3 / 3 页");
    expect(wrapper.text()).toContain("第三页公司");
    expect(wrapper.text()).not.toContain("第二页公司");
  });

  it("keeps the latest selected detail when detail requests resolve out of order", async () => {
    const firstDetail = deferred<JobDetail>();
    const secondDetail = deferred<JobDetail>();
    const secondJob = jobWith("job-2", "第二家公司");
    listMock.mockResolvedValue({ total: 2, jobs: [job, secondJob] });
    detailMock
      .mockReset()
      .mockReturnValueOnce(firstDetail.promise)
      .mockReturnValueOnce(secondDetail.promise);

    const wrapper = mount(JobCenter, { props: { token: "student-token" } });
    await flushPromises();

    await wrapper.get('[data-test="show-detail-job-1"]').trigger("click");
    await wrapper.get('[data-test="show-detail-job-2"]').trigger("click");

    secondDetail.resolve({
      ...detail,
      ...secondJob,
      description_text: "最新选择的职位详情。",
    });
    await flushPromises();
    expect(wrapper.text()).toContain("最新选择的职位详情");

    firstDetail.resolve({ ...detail, description_text: "已经过期的职位详情。" });
    await flushPromises();
    expect(wrapper.text()).toContain("最新选择的职位详情");
    expect(wrapper.text()).not.toContain("已经过期的职位详情");
  });

  it("does not read a list response that resolves after unmount", async () => {
    const pendingResponse = deferred<JobListResponse>();
    let jobsReadCount = 0;
    const response = {
      total: 1,
      get jobs() {
        jobsReadCount += 1;
        return [job];
      },
    };
    listMock.mockReset().mockReturnValue(pendingResponse.promise);

    const wrapper = mount(JobCenter, { props: { token: "student-token" } });
    wrapper.unmount();
    pendingResponse.resolve(response);
    await flushPromises();

    expect(jobsReadCount).toBe(0);
  });

  it("does not render sensitive fields even when a runtime payload contains them", async () => {
    const sentinels = [
      "SOURCE_CANDIDATE_SENTINEL",
      "REVIEW_VERSION_SENTINEL",
      "RAW_FIELDS_SENTINEL",
      "TOKEN_SENTINEL",
      "TRACE_SENTINEL",
    ];
    const unsafeFields = {
      source_candidate: sentinels[0],
      review_version: sentinels[1],
      raw_fields: sentinels[2],
      token: sentinels[3],
      trace: sentinels[4],
    };
    listMock.mockResolvedValue({
      total: 1,
      jobs: [{ ...job, ...unsafeFields } as unknown as JobSummary],
    });
    detailMock.mockResolvedValue({ ...detail, ...unsafeFields } as unknown as JobDetail);

    const wrapper = mount(JobCenter, { props: { token: "student-token" } });
    await flushPromises();
    await wrapper.get('[data-test="show-detail-job-1"]').trigger("click");
    await flushPromises();

    for (const sentinel of sentinels) {
      expect(wrapper.text()).not.toContain(sentinel);
    }
  });
});
