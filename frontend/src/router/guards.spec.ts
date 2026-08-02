import { beforeEach, describe, expect, it, vi } from "vitest";

const state = vi.hoisted(() => ({
  loading: { value: false },
  isAuthenticated: { value: false },
  isAdmin: { value: false },
}));
vi.mock("../state/auth", () => ({ useAuth: () => state }));

import { applyGuards } from "./guards";

describe("route guards", () => {
  let guard: (to: any, from: any, next: (path?: string) => void) => Promise<void>;

  beforeEach(() => {
    state.loading.value = false;
    state.isAuthenticated.value = false;
    state.isAdmin.value = false;
    applyGuards({ beforeEach: vi.fn((callback) => { guard = callback; }) } as any);
  });

  it("redirects unauthenticated protected routes to login", async () => {
    const next = vi.fn();
    await guard({ meta: { requiresAuth: true } }, {}, next);
    expect(next).toHaveBeenCalledWith("/login");
  });

  it("redirects non-admin routes requiring administration to the home route", async () => {
    state.isAuthenticated.value = true;
    const next = vi.fn();
    await guard({ meta: { requiresAdmin: true } }, {}, next);
    expect(next).toHaveBeenCalledWith("/");
  });

  it("allows public and authorized routes", async () => {
    const publicNext = vi.fn();
    await guard({ meta: {} }, {}, publicNext);
    state.isAuthenticated.value = true;
    state.isAdmin.value = true;
    const adminNext = vi.fn();
    await guard({ meta: { requiresAuth: true, requiresAdmin: true } }, {}, adminNext);
    expect(publicNext).toHaveBeenCalledWith();
    expect(adminNext).toHaveBeenCalledWith();
  });

  it("waits for bootstrap before deciding a protected route", async () => {
    state.loading.value = true;
    const next = vi.fn();
    setTimeout(() => {
      state.loading.value = false;
      state.isAuthenticated.value = true;
    }, 5);

    await guard({ meta: { requiresAuth: true } }, {}, next);

    expect(next).toHaveBeenCalledWith();
  });

  it("keeps polling while bootstrap is still loading, then proceeds", async () => {
    state.loading.value = true;
    const next = vi.fn();
    // Resolve loading only AFTER the first 50ms interval tick has already
    // fired while still loading -> exercises the ``if (!auth.loading.value)``
    // False arm (keep polling) before the True arm resolves the wait.
    setTimeout(() => {
      state.loading.value = false;
    }, 70);

    await guard({ meta: {} }, {}, next);

    expect(next).toHaveBeenCalledWith();
  });
});
