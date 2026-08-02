import { beforeEach, describe, expect, it, vi } from "vitest";

const api = vi.hoisted(() => ({ login: vi.fn(), register: vi.fn(), fetchMe: vi.fn() }));
vi.mock("../api", () => api);

describe("auth state", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.clearAllMocks();
    localStorage.clear();
  });

  it("bootstraps a stored token into an authenticated user", async () => {
    localStorage.setItem("job_assistant_token", "saved");
    api.fetchMe.mockResolvedValue({ account: "student", nickname: "Student", role: "student" });
    const { useAuth } = await import("./auth");
    const auth = useAuth();

    await auth.bootstrap();

    expect(auth.isAuthenticated.value).toBe(true);
    expect(auth.isAdmin.value).toBe(false);
    expect(auth.user.value?.id).toBe("student");
    expect(auth.loading.value).toBe(false);
  });

  it("clears a rejected stored token", async () => {
    localStorage.setItem("job_assistant_token", "expired");
    api.fetchMe.mockRejectedValue(new Error("expired"));
    const { useAuth } = await import("./auth");

    await useAuth().bootstrap();

    expect(localStorage.getItem("job_assistant_token")).toBeNull();
    expect(useAuth().isAuthenticated.value).toBe(false);
  });

  it("stores valid login and registration responses and rejects incomplete ones", async () => {
    const { useAuth } = await import("./auth");
    const auth = useAuth();
    api.login.mockResolvedValue({ ok: false, message: "denied" });
    await expect(auth.login("a", "p")).rejects.toThrow("denied");
    api.login.mockResolvedValue({ ok: true, token: "login", profile: { account: "a", nickname: "A", role: "admin" } });
    await auth.login("a", "p");
    expect(auth.isAdmin.value).toBe(true);
    api.register.mockResolvedValue({ ok: true, token: "register", profile: { account: "b", nickname: "B", role: "student" } });
    await auth.register("b", "B", "p");
    expect(localStorage.getItem("job_assistant_token")).toBe("register");
    auth.logout();
    expect(auth.user.value).toBeNull();
  });

  it("uses default errors for incomplete registration and supports an empty bootstrap", async () => {
    const { useAuth } = await import("./auth");
    const auth = useAuth();
    await auth.bootstrap();
    expect(auth.token.value).toBeNull();
    expect(auth.loading.value).toBe(false);

    api.register.mockResolvedValue({ ok: false });
    await expect(auth.register("b", "B", "p")).rejects.toThrow("注册失败");
  });

  it("uses a default error message when a login response omits one", async () => {
    const { useAuth } = await import("./auth");
    const auth = useAuth();
    api.login.mockResolvedValue({ ok: false });
    await expect(auth.login("a", "p")).rejects.toThrow("登录失败");
  });
});
