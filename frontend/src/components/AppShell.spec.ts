import { mount } from "@vue/test-utils";
import { beforeEach, describe, expect, it, vi } from "vitest";

const router = vi.hoisted(() => ({ push: vi.fn() }));
const auth = vi.hoisted(() => ({
  user: null as { nickname: string; role: string } | null,
  token: null as string | null,
  isAuthenticated: false,
  logout: vi.fn(),
}));
vi.mock("vue-router", () => ({ useRouter: () => router }));
vi.mock("../state/auth", () => ({ useAuth: () => auth }));

import AppShell from "./AppShell.vue";

describe("application shell", () => {
  const stubs = {
    "router-view": true,
    "router-link": { template: "<a><slot /></a>" },
  };
  beforeEach(() => {
    vi.clearAllMocks();
    auth.user = null;
    auth.token = null;
    auth.isAuthenticated = false;
  });

  it("keeps navigation hidden for an anonymous visitor", () => {
    const wrapper = mount(AppShell, { global: { stubs } });
    expect(wrapper.find("nav").exists()).toBe(false);
    expect(wrapper.find("main").classes()).toContain("auth-main");
  });

  it("shows only the personal assistant navigation for an authenticated user", () => {
    auth.user = { nickname: "Student", role: "student" };
    auth.token = "token";
    auth.isAuthenticated = true;
    const wrapper = mount(AppShell, { global: { stubs } });
    expect(wrapper.text()).toContain("Assistant");
    expect(wrapper.text()).toContain("Profile");
    expect(wrapper.text()).toContain("Student (student)");
  });

  it("logs out before routing to login", async () => {
    auth.isAuthenticated = true;
    const wrapper = mount(AppShell, { global: { stubs } });
    await wrapper.get("button").trigger("click");
    expect(auth.logout).toHaveBeenCalledOnce();
    expect(router.push).toHaveBeenCalledWith("/login");
  });
});
