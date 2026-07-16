import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

import App from "../App.vue";

const profile = {
  account: "student",
  nickname: "同学",
  role: "student" as const,
  created_at: "2026-07-16T00:00:00Z",
  last_login_at: "2026-07-16T00:00:00Z",
  active_thread_id: "thread-1",
  sessions: [],
};

const apiMocks = vi.hoisted(() => ({
  activateSession: vi.fn(),
  createSession: vi.fn(),
  fetchMe: vi.fn(),
  fetchSessionHistory: vi.fn(),
  fetchSessionState: vi.fn(),
  fetchSessions: vi.fn(),
  login: vi.fn(),
  register: vi.fn(),
  request: vi.fn(),
  runAnalysis: vi.fn(),
}));

vi.mock("../api", () => apiMocks);

describe("App workspace navigation", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    localStorage.setItem("job_assistant_token", "student-token");
    apiMocks.fetchMe.mockResolvedValue(profile);
    apiMocks.fetchSessions.mockResolvedValue({
      active_thread_id: "thread-1",
      sessions: [],
    });
    apiMocks.fetchSessionState.mockResolvedValue({
      thread_id: "thread-1",
      values: {},
      summary: {
        user_goal: "",
        jobs_count: 0,
        analyses_count: 0,
        matches_count: 0,
        optimization_round: 0,
        has_final_report: false,
        shortlist: [],
        revision_notes: [],
      },
    });
    apiMocks.request.mockResolvedValue({ total: 0, jobs: [] });
    apiMocks.login.mockResolvedValue({
      ok: true,
      message: "登录成功",
      token: "student-token-2",
      profile,
    });
  });

  it("preserves the analysis workbench while switching to the job center", async () => {
    const wrapper = mount(App);
    await flushPromises();

    expect(wrapper.text()).toContain("运行分析");
    await wrapper.get('[data-test="jobs-view"]').trigger("click");
    await flushPromises();
    expect(wrapper.text()).toContain("已核验职位");
    expect(wrapper.get('[data-test="analysis-workspace"]').isVisible()).toBe(false);

    await wrapper.get('[data-test="analysis-view"]').trigger("click");
    expect(wrapper.get('[data-test="analysis-workspace"]').attributes("style") || "").not.toContain(
      "display: none",
    );
    expect(wrapper.text()).toContain("运行分析");
  });

  it("resets the workspace view to analysis after logout and login", async () => {
    const wrapper = mount(App);
    await flushPromises();
    await wrapper.get('[data-test="jobs-view"]').trigger("click");

    await wrapper.get(".danger-button").trigger("click");
    expect(wrapper.text()).toContain("进入工作台");

    await wrapper.get('input[placeholder="例如 lichunfeng"]').setValue("student");
    await wrapper.get('input[type="password"]').setValue("password");
    await wrapper.get(".primary-button").trigger("click");
    await flushPromises();

    expect(wrapper.get('[data-test="analysis-workspace"]').isVisible()).toBe(true);
    expect(wrapper.text()).toContain("运行分析");
  });
});
