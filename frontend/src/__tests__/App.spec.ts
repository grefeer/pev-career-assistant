import { flushPromises, mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { createRouter, createWebHistory } from "vue-router";

import App from "../App.vue";
import { routes } from "../router/index";
import { applyGuards } from "../router/guards";
import { useAuth } from "../state/auth";

const profile = {
  account: "student",
  nickname: "同学",
  role: "student" as const,
  created_at: "2026-07-16T00:00:00Z",
  last_login_at: "2026-07-16T00:00:00Z",
  active_thread_id: "thread-1",
  sessions: [],
};

const adminProfile = {
  ...profile,
  account: "admin",
  nickname: "管理员",
  role: "admin" as const,
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
}));

vi.mock("../api", () => apiMocks);

function createRouterForTest() {
  const r = createRouter({
    history: createWebHistory(),
    routes,
  });
  applyGuards(r);
  return r;
}

describe("App root renders AppShell", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    apiMocks.request.mockResolvedValue({ total: 0, jobs: [] });
    apiMocks.login.mockResolvedValue({
      ok: true,
      message: "登录成功",
      token: "student-token-2",
      profile,
    });
  });

  it("guard redirects unauthenticated users to login", async () => {
    const auth = useAuth();
    await auth.bootstrap(); // No token in localStorage -> user=null, token=null

    expect(auth.isAuthenticated.value).toBe(false);
    expect(auth.loading.value).toBe(false);

    const router = createRouterForTest();
    await router.push("/matching");
    await flushPromises();

    expect(router.currentRoute.value.path).toBe("/login");
  });

  it("renders AppShell with nav links when authenticated", async () => {
    localStorage.setItem("job_assistant_token", "student-token");
    apiMocks.fetchMe.mockResolvedValue(profile);
    apiMocks.fetchSessions.mockResolvedValue({
      active_thread_id: "thread-1",
      sessions: [],
    });

    const auth = useAuth();
    await auth.bootstrap();
    const router = createRouterForTest();
    await router.push("/matching");

    const wrapper = mount(App, {
      global: { plugins: [router] },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("Match");
    expect(wrapper.text()).toContain("Jobs");
    expect(wrapper.text()).toContain("Profile");
    expect(wrapper.text()).toContain("同学");
    expect(wrapper.text()).toContain("student");
  });

  it("passes the auth token into routed workspace components", async () => {
    localStorage.setItem("job_assistant_token", "student-token");
    apiMocks.fetchMe.mockResolvedValue(profile);
    apiMocks.request.mockImplementation((path: string) => {
      if (path === "/profiles") {
        return Promise.resolve({ version: 0, evidence: [] });
      }
      if (path === "/resume-assets") {
        return Promise.resolve({ assets: [] });
      }
      if (path === "/profile-versions") {
        return Promise.resolve({ versions: [] });
      }
      return Promise.resolve({});
    });

    const auth = useAuth();
    await auth.bootstrap();
    const router = createRouterForTest();
    await router.push("/profile");

    mount(App, {
      global: { plugins: [router] },
    });
    await flushPromises();

    expect(apiMocks.request).toHaveBeenCalledWith("/profiles", {}, "student-token");
    expect(apiMocks.request).toHaveBeenCalledWith("/resume-assets", {}, "student-token");
    expect(apiMocks.request).toHaveBeenCalledWith("/profile-versions", {}, "student-token");
  });

  it("shows admin links only for admin users", async () => {
    localStorage.setItem("job_assistant_token", "admin-token");
    apiMocks.fetchMe.mockResolvedValue(adminProfile);
    apiMocks.fetchSessions.mockResolvedValue({
      active_thread_id: "thread-1",
      sessions: [],
    });

    const auth = useAuth();
    await auth.bootstrap();
    const router = createRouterForTest();
    await router.push("/matching");

    const wrapper = mount(App, {
      global: { plugins: [router] },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("Admin Jobs");
    expect(wrapper.text()).toContain("Admin Submissions");
    expect(wrapper.text()).toContain("Admin Feedback");
  });

  it("does not show admin links for non-admin users", async () => {
    localStorage.setItem("job_assistant_token", "student-token");
    apiMocks.fetchMe.mockResolvedValue(profile);
    apiMocks.fetchSessions.mockResolvedValue({
      active_thread_id: "thread-1",
      sessions: [],
    });

    const auth = useAuth();
    await auth.bootstrap();
    const router = createRouterForTest();
    await router.push("/matching");

    const wrapper = mount(App, {
      global: { plugins: [router] },
    });
    await flushPromises();

    expect(wrapper.text()).not.toContain("Admin Jobs");
    expect(wrapper.text()).not.toContain("Admin Submissions");
    expect(wrapper.text()).not.toContain("Admin Feedback");
  });

  it("redirects to /login when not authenticated", async () => {
    // No token - bootstrap will set loading=false with no user
    const auth = useAuth();
    await auth.bootstrap();
    const router = createRouterForTest();

    const wrapper = mount(App, {
      global: { plugins: [router] },
    });
    await flushPromises();
    await new Promise((r) => setTimeout(r, 50));
    await flushPromises();

    expect(wrapper.text()).toContain("登录");
    expect(wrapper.text()).toContain("进入工作台");
  });

  it("shows LoginPage after logout and re-navigation", async () => {
    localStorage.setItem("job_assistant_token", "student-token");
    apiMocks.fetchMe.mockResolvedValue(profile);
    apiMocks.fetchSessions.mockResolvedValue({
      active_thread_id: "thread-1",
      sessions: [],
    });

    const auth = useAuth();
    await auth.bootstrap();
    const router = createRouterForTest();
    await router.push("/matching");

    const wrapper = mount(App, {
      global: { plugins: [router] },
    });
    await flushPromises();

    // Logout and verify auth state
    auth.logout();
    expect(auth.isAuthenticated.value).toBe(false);
    expect(auth.token.value).toBeNull();
    expect(auth.user.value).toBeNull();

    // Re-navigate to a different auth-required route to trigger guard redirect
    await router.push("/jobs").catch(() => {});
    await flushPromises();
    await new Promise((r) => setTimeout(r, 100));
    await flushPromises();

    // Verify the router redirected to login
    expect(router.currentRoute.value.path).toBe("/login");
    expect(wrapper.text()).toContain("登录");
    expect(wrapper.text()).toContain("进入工作台");
    expect(wrapper.text()).not.toContain("Match");
    expect(wrapper.text()).not.toContain("Jobs");
    expect(wrapper.text()).not.toContain("Logout");
  });

  it("allows login via LoginPage", async () => {
    apiMocks.fetchMe.mockResolvedValue(profile);
    apiMocks.fetchSessions.mockResolvedValue({
      active_thread_id: "thread-1",
      sessions: [],
    });

    // No token initially
    const auth = useAuth();
    await auth.bootstrap();
    const router = createRouterForTest();
    await router.push("/login");

    const wrapper = mount(App, {
      global: { plugins: [router] },
    });
    await flushPromises();

    expect(wrapper.text()).toContain("进入工作台");
    await wrapper.get('input[placeholder="例如 lichunfeng"]').setValue("student");
    await wrapper.get('input[type="password"]').setValue("password");
    await wrapper.get(".primary-button").trigger("click");
    await flushPromises();

    expect(apiMocks.login).toHaveBeenCalledWith({
      account: "student",
      password: "password",
    });
  });
});
