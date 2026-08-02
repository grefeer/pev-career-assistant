import { afterEach, describe, expect, it, vi } from "vitest"

import { createAgentRun, fetchAgentRuns, resumeAgentRun } from "../agentRuntimeApi"

describe("agent runtime API", () => {
  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it("sends only the public goal, Skill authority and non-empty source hints", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({
        id: "run-1", status: "succeeded", summary: "已完成", error_code: null,
      }), { status: 201, headers: { "Content-Type": "application/json" } }),
    )
    vi.stubGlobal("fetch", fetchMock)

    await createAgentRun("student-token", {
      goal: "找最近三天的 AI Agent 岗位",
      allowed_skills: ["job-discovery", "job-matching"],
      candidate_urls: [" https://jobs.example/1 ", "   "],
    })

    expect(fetchMock.mock.calls[0][0]).toBe("/api/agent-runs")
    expect(fetchMock.mock.calls[0][1].method).toBe("POST")
    expect(new Headers(fetchMock.mock.calls[0][1].headers).get("Authorization")).toBe(
      "Bearer student-token",
    )
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      goal: "找最近三天的 AI Agent 岗位",
      allowed_skills: ["job-discovery", "job-matching"],
      context: { candidate_urls: ["https://jobs.example/1"] },
    })
  })

  it("loads a bounded owner-scoped run history", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [] }), {
        status: 200, headers: { "Content-Type": "application/json" },
      }),
    )
    vi.stubGlobal("fetch", fetchMock)

    await fetchAgentRuns("student-token", 12)

    expect(fetchMock.mock.calls[0][0]).toBe("/api/agent-runs?limit=12")
  })

  it("resumes a waiting run with only the human clarification", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({
        id: "run-1", status: "running", summary: null, error_code: null,
      }), { status: 200, headers: { "Content-Type": "application/json" } }),
    )
    vi.stubGlobal("fetch", fetchMock)

    await resumeAgentRun("student-token", "run-1", "北京")

    expect(fetchMock.mock.calls[0][0]).toBe("/api/agent-runs/run-1/resume")
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ user_response: "北京" })
  })
})
