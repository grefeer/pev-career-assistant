import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, fetchMe, login, register, request } from "../api";

describe("shared API client", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("serializes JSON requests and bearer credentials", async () => {
    const fetchMock = vi.fn().mockImplementation(() => new Response(JSON.stringify({ ok: true })));
    vi.stubGlobal("fetch", fetchMock);

    await request("/safe", { method: "POST", body: JSON.stringify({ x: 1 }) }, "token");

    const headers = new Headers(fetchMock.mock.calls[0][1].headers);
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(headers.get("Authorization")).toBe("Bearer token");
  });

  it("keeps multipart requests free of a JSON content type", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true })));
    vi.stubGlobal("fetch", fetchMock);
    const form = new FormData();
    form.append("file", new Blob(["resume"]), "resume.pdf");

    await request("/upload", { method: "POST", body: form });

    expect(new Headers(fetchMock.mock.calls[0][1].headers).has("Content-Type")).toBe(false);
  });

  it.each([
    [{ detail: "明确错误" }, "明确错误"],
    [{ detail: { message: "安全消息" } }, "安全消息"],
    [{ detail: { code: "not_found" } }, "not_found"],
    [{ detail: { error_code: "budget_exhausted" } }, "budget_exhausted"],
    [{ message: "备用消息" }, "备用消息"],
    [{ detail: { unknown: true } }, "请求失败：418"],
  ])("normalizes public error payload %o", async (payload, expectedMessage) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify(payload), { status: 418 }),
    ));

    await expect(request("/failing")).rejects.toMatchObject<ApiError>({
      status: 418,
      message: expectedMessage,
    });
  });

  it("uses fallback error text when an error response has no JSON", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("broken", { status: 500 })));

    await expect(request("/failing")).rejects.toMatchObject<ApiError>({
      status: 500,
      detail: null,
      message: "请求失败：500",
    });
  });

  it("uses the three auth endpoint contracts", async () => {
    const fetchMock = vi.fn().mockImplementation(() => new Response(JSON.stringify({ ok: true })));
    vi.stubGlobal("fetch", fetchMock);

    await register({ account: "a", nickname: "A", password: "secret" });
    await login({ account: "a", password: "secret" });
    await fetchMe("token");

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/auth/register", "/api/auth/login", "/api/auth/me",
    ]);
  });
});
