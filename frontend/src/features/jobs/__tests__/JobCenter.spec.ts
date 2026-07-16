import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import JobCenter from "../JobCenter.vue";
import { fetchVerifiedJob, fetchVerifiedJobs } from "../jobsApi";
import type { JobDetail, JobSummary } from "../jobTypes";

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
});
