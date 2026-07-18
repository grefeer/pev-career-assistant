import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import DiscoveryReview from "../DiscoveryReview.vue";
import {
  approveJobDiscoveryCandidate,
  fetchJobDiscoveryGroups,
  fetchJobDiscoveryTasks,
  rejectJobDiscoveryCandidate,
  retryJobDiscoveryTask,
} from "../jobsApi";
import type { DiscoveredJobCandidate, JobDiscoveryReviewGroup, JobDiscoveryTask } from "../jobTypes";

vi.mock("../jobsApi", () => ({
  fetchJobDiscoveryTasks: vi.fn(),
  fetchJobDiscoveryGroups: vi.fn(),
  retryJobDiscoveryTask: vi.fn(),
  approveJobDiscoveryCandidate: vi.fn(),
  rejectJobDiscoveryCandidate: vi.fn(),
}));

const sampleTask: JobDiscoveryTask = {
  id: "task-1",
  source_key: "tencent-27-referrals",
  source_name: "27届内推",
  source_url: "https://careers.example.com/job/123",
  status: "succeeded",
  block_reason: null,
  attempt_count: 1,
  result_summary_json: { candidates_found: 3 },
  created_at: "2026-07-18T00:00:00Z",
  updated_at: "2026-07-18T01:00:00Z",
};

const blockedTask: JobDiscoveryTask = {
  id: "task-2",
  source_key: "tencent-intern-referrals",
  source_name: "实习内推",
  source_url: "https://careers.example.com/job/456",
  status: "needs_manual_review",
  block_reason: "login_required",
  attempt_count: 2,
  result_summary_json: null,
  created_at: "2026-07-18T02:00:00Z",
  updated_at: "2026-07-18T03:00:00Z",
};

const runningTask: JobDiscoveryTask = {
  id: "task-3",
  source_key: "tencent-27-referrals",
  source_name: "27届内推",
  source_url: "https://careers.example.com/job/789",
  status: "running",
  block_reason: null,
  attempt_count: 0,
  result_summary_json: null,
  created_at: "2026-07-18T04:00:00Z",
  updated_at: "2026-07-18T04:00:00Z",
};

const sampleCandidate: DiscoveredJobCandidate = {
  id: "candidate-1",
  task_id: "task-1",
  similarity_group_key: "group-a",
  status: "pending_review",
  title: "后端开发实习生",
  company_name: "示例科技",
  description_text: "负责后端服务开发和维护。",
  locations_json: ["上海", "杭州"],
  apply_url: "https://careers.example.com/apply/123",
  confidence: 0.85,
  evidence_refs_json: [
    { url: "https://example.com/page1", title: "官方招聘页", excerpt: "后端开发实习生岗位" },
  ],
  normalization_warnings_json: ["公司名来自URL而非页面内容"],
  created_at: "2026-07-18T00:00:00Z",
};

const approvedCandidate: DiscoveredJobCandidate = {
  ...sampleCandidate,
  id: "candidate-2",
  status: "approved",
  confidence: 0.72,
};

const rejectedCandidate: DiscoveredJobCandidate = {
  ...sampleCandidate,
  id: "candidate-3",
  status: "rejected",
  title: "前端开发实习生",
  confidence: 0.35,
};

const sampleGroup: JobDiscoveryReviewGroup = {
  similarity_group_key: "group-a",
  candidates: [sampleCandidate, approvedCandidate, rejectedCandidate],
};

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe("DiscoveryReview", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(fetchJobDiscoveryTasks).mockResolvedValue({ tasks: [sampleTask, blockedTask, runningTask] });
    vi.mocked(fetchJobDiscoveryGroups).mockResolvedValue([sampleGroup]);
    vi.mocked(retryJobDiscoveryTask).mockResolvedValue({
      ...sampleTask,
      status: "queued",
      attempt_count: 0,
    });
    vi.mocked(approveJobDiscoveryCandidate).mockResolvedValue({
      ...sampleCandidate,
      status: "approved",
    });
    vi.mocked(rejectJobDiscoveryCandidate).mockResolvedValue({
      ...sampleCandidate,
      status: "rejected",
    });
  });

  it("renders discovery tasks with source name and status on mount", async () => {
    const wrapper = mount(DiscoveryReview, { props: { token: "admin-token" } });
    await flushPromises();

    expect(fetchJobDiscoveryTasks).toHaveBeenCalledWith("admin-token");
    expect(wrapper.text()).toContain("27届内推");
    expect(wrapper.text()).toContain("实习内推");
  });

  it("shows task status labels and block reason", async () => {
    const wrapper = mount(DiscoveryReview, { props: { token: "admin-token" } });
    await flushPromises();

    expect(wrapper.text()).toContain("已完成");
    const statusBadges = wrapper.findAll('[data-test="task-status"]');
    expect(statusBadges.length).toBeGreaterThanOrEqual(1);
    expect(wrapper.text()).toContain("login_required");
  });

  it("renders retry button for non-running tasks and hides it for running tasks", async () => {
    const wrapper = mount(DiscoveryReview, { props: { token: "admin-token" } });
    await flushPromises();

    // Task cards should exist
    const taskCards = wrapper.findAll('[data-test^="task-"]');
    expect(taskCards.length).toBeGreaterThanOrEqual(3);

    // retry buttons exist for non-running tasks
    const retryButtons = wrapper.findAll('[data-test="retry-task"]');
    expect(retryButtons.length).toBe(2);
  });

  it("switches to groups tab and renders grouped candidates", async () => {
    const wrapper = mount(DiscoveryReview, { props: { token: "admin-token" } });
    await flushPromises();

    // Click the groups tab button
    const groupButtons = wrapper.findAll("button");
    const groupsTab = groupButtons.find((b) => b.text().includes("审核分组"));
    expect(groupsTab).toBeDefined();
    if (groupsTab) {
      await groupsTab.trigger("click");
      await flushPromises();

      expect(fetchJobDiscoveryGroups).toHaveBeenCalledWith("admin-token");
      expect(wrapper.text()).toContain("group-a");
      expect(wrapper.text()).toContain("示例科技");
    }
  });

  it("calls approve API when approve button is clicked", async () => {
    const wrapper = mount(DiscoveryReview, { props: { token: "admin-token" } });
    await flushPromises();

    // Switch to groups tab
    const groupsTab = wrapper.findAll("button").find((b) => b.text().includes("审核分组"));
    expect(groupsTab).toBeDefined();
    if (groupsTab) {
      await groupsTab.trigger("click");
      await flushPromises();

      // Click approve button
      const approveButtons = wrapper.findAll('[data-test="approve-candidate"]');
      expect(approveButtons.length).toBeGreaterThanOrEqual(1);
      await approveButtons[0].trigger("click");
      await flushPromises();

      expect(approveJobDiscoveryCandidate).toHaveBeenCalledWith("admin-token", "candidate-1");
    }
  });

  it("calls reject API when reject button is clicked", async () => {
    const wrapper = mount(DiscoveryReview, { props: { token: "admin-token" } });
    await flushPromises();

    // Switch to groups tab
    const groupsTab = wrapper.findAll("button").find((b) => b.text().includes("审核分组"));
    expect(groupsTab).toBeDefined();
    if (groupsTab) {
      await groupsTab.trigger("click");
      await flushPromises();

      // Click reject button
      const rejectButtons = wrapper.findAll('[data-test="reject-candidate"]');
      expect(rejectButtons.length).toBeGreaterThanOrEqual(1);
      await rejectButtons[0].trigger("click");
      await flushPromises();

      expect(rejectJobDiscoveryCandidate).toHaveBeenCalledWith("admin-token", "candidate-1");
    }
  });

  it("calls retry API when retry button is clicked", async () => {
    const wrapper = mount(DiscoveryReview, { props: { token: "admin-token" } });
    await flushPromises();

    // Click a retry button
    const retryButtons = wrapper.findAll('[data-test="retry-task"]');
    expect(retryButtons.length).toBeGreaterThanOrEqual(1);
    await retryButtons[0].trigger("click");
    await flushPromises();

    expect(retryJobDiscoveryTask).toHaveBeenCalledWith("admin-token", "task-1");
  });

  it("shows evidence section for candidates with evidence", async () => {
    vi.mocked(fetchJobDiscoveryGroups).mockResolvedValue([sampleGroup]);
    const wrapper = mount(DiscoveryReview, { props: { token: "admin-token" } });
    await flushPromises();

    // Switch to groups tab
    const groupsTab = wrapper.findAll("button").find((b) => b.text().includes("审核分组"));
    if (groupsTab) {
      await groupsTab.trigger("click");
      await flushPromises();

      const evidenceSections = wrapper.findAll('[data-test="evidence-section"]');
      expect(evidenceSections.length).toBeGreaterThanOrEqual(1);
      expect(wrapper.text()).toContain("官方招聘页");
    }
  });

  it("shows normalization warnings for candidates", async () => {
    vi.mocked(fetchJobDiscoveryGroups).mockResolvedValue([sampleGroup]);
    const wrapper = mount(DiscoveryReview, { props: { token: "admin-token" } });
    await flushPromises();

    // Switch to groups tab
    const groupsTab = wrapper.findAll("button").find((b) => b.text().includes("审核分组"));
    if (groupsTab) {
      await groupsTab.trigger("click");
      await flushPromises();

      const warningSections = wrapper.findAll('[data-test="warnings-section"]');
      expect(warningSections.length).toBeGreaterThanOrEqual(1);
      expect(wrapper.text()).toContain("公司名来自URL");
    }
  });

  it("handles API errors gracefully", async () => {
    vi.mocked(fetchJobDiscoveryTasks).mockRejectedValue(new Error("network error"));
    const wrapper = mount(DiscoveryReview, { props: { token: "admin-token" } });
    await flushPromises();

    expect(wrapper.text()).toContain("操作失败");
  });

  it("refreshes tasks when refresh button is clicked", async () => {
    const wrapper = mount(DiscoveryReview, { props: { token: "admin-token" } });
    await flushPromises();

    const refreshBtn = wrapper.find('[data-test="refresh-tasks"]');
    expect(refreshBtn.exists()).toBe(true);
    await refreshBtn.trigger("click");
    await flushPromises();

    expect(fetchJobDiscoveryTasks).toHaveBeenCalledTimes(2);
  });
});
