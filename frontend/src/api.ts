import type {
  AuthResponse,
  HistoryItem,
  SessionItem,
  SessionStateResponse,
  UserProfile,
} from "./types";

const API_BASE = "/api";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: unknown,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export async function request<T>(
  path: string,
  init: RequestInit = {},
  token?: string,
): Promise<T> {
  const headers = new Headers(init.headers || {});
  if (!(init.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers,
  });

  if (!response.ok) {
    let detail: unknown = null;
    let message = `请求失败：${response.status}`;
    try {
      const data = (await response.json()) as unknown;
      if (data && typeof data === "object") {
        const body = data as Record<string, unknown>;
        detail = body.detail ?? body.message ?? null;
      }
      if (typeof detail === "string" && detail.trim()) {
        message = detail;
      } else if (detail && typeof detail === "object") {
        const structuredDetail = detail as Record<string, unknown>;
        if (typeof structuredDetail.message === "string" && structuredDetail.message.trim()) {
          message = structuredDetail.message;
        } else if (
          typeof structuredDetail.code === "string"
          && structuredDetail.code.trim()
        ) {
          message = structuredDetail.code;
        } else if (
          typeof structuredDetail.error_code === "string" &&
          structuredDetail.error_code.trim()
        ) {
          message = structuredDetail.error_code;
        }
      }
    } catch {
      detail = null;
    }
    throw new ApiError(response.status, detail, message);
  }

  return (await response.json()) as T;
}

export function register(payload: {
  account: string;
  nickname: string;
  password: string;
}): Promise<AuthResponse> {
  return request<AuthResponse>("/auth/register", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function login(payload: {
  account: string;
  password: string;
}): Promise<AuthResponse> {
  return request<AuthResponse>("/auth/login", {
    method: "POST",
    body: JSON.stringify(payload),
  });
}

export function fetchMe(token: string): Promise<UserProfile> {
  return request<UserProfile>("/auth/me", {}, token);
}

export async function fetchSessions(token: string): Promise<{
  active_thread_id: string;
  sessions: SessionItem[];
}> {
  return request("/sessions", {}, token);
}

export async function createSession(token: string): Promise<{ ok: boolean; active_thread_id: string }> {
  return request("/sessions", { method: "POST" }, token);
}

export async function activateSession(
  token: string,
  threadId: string,
): Promise<{ ok: boolean; active_thread_id: string }> {
  return request(`/sessions/${threadId}/activate`, { method: "POST" }, token);
}

export function fetchSessionState(token: string, threadId: string): Promise<SessionStateResponse> {
  return request<SessionStateResponse>(`/sessions/${threadId}`, {}, token);
}

export function fetchSessionHistory(
  token: string,
  threadId: string,
  limit = 10,
): Promise<HistoryItem[]> {
  return request<HistoryItem[]>(`/sessions/${threadId}/history?limit=${limit}`, {}, token);
}
