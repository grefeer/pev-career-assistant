import { afterEach, describe, expect, it, vi } from "vitest"

import {
  createAgentRun,
  fetchAgentRun,
  fetchAgentRunArtifacts,
  fetchAgentRunEvents,
  fetchAgentRunPlans,
  fetchAgentRuns,
  recoverAgentRun,
  resumeAgentRun,
  streamAgentRunEvents,
} from "../agentRuntimeApi"

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

  it("loads only the safe plan projection for one owner-scoped run", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ items: [] }), {
        status: 200, headers: { "Content-Type": "application/json" },
      }),
    )
    vi.stubGlobal("fetch", fetchMock)

    await fetchAgentRunPlans("student-token", "run/1")

    expect(fetchMock.mock.calls[0][0]).toBe("/api/agent-runs/run%2F1/plans")
  })

  it("encodes an owner run identifier for run, events, and artifacts", async () => {
    const fetchMock = vi.fn().mockImplementation(() => new Response(JSON.stringify({ items: [] }), {
      status: 200, headers: { "Content-Type": "application/json" },
    }))
    vi.stubGlobal("fetch", fetchMock)

    await fetchAgentRun("student-token", "run/1")
    await fetchAgentRunEvents("student-token", "run/1")
    await fetchAgentRunArtifacts("student-token", "run/1")

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "/api/agent-runs/run%2F1",
      "/api/agent-runs/run%2F1/events",
      "/api/agent-runs/run%2F1/artifacts",
    ])
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

  it("recovers a running run with an explicitly empty body", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({
        id: "run-1", status: "succeeded", summary: "恢复完成", error_code: null,
      }), { status: 200, headers: { "Content-Type": "application/json" } }),
    )
    vi.stubGlobal("fetch", fetchMock)

    await recoverAgentRun("student-token", "run-1")

    expect(fetchMock.mock.calls[0][0]).toBe("/api/agent-runs/run-1/recover")
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({})
  })

  it("consumes the authenticated durable SSE stream from its last event cursor", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        'id: 3\nevent: plan_created\ndata: {"sequence":3,"event_type":"plan_created","payload":{"revision":1},"created_at":"2026-08-02T00:00:00Z"}\n\n',
        { status: 200, headers: { "Content-Type": "text/event-stream" } },
      ),
    )
    vi.stubGlobal("fetch", fetchMock)
    const received: unknown[] = []

    await streamAgentRunEvents("student-token", "run/1", 2, undefined, (event) => received.push(event))

    expect(fetchMock.mock.calls[0][0]).toBe("/api/agent-runs/run%2F1/events/stream?after_sequence=2")
    expect(new Headers(fetchMock.mock.calls[0][1].headers).get("Authorization")).toBe("Bearer student-token")
    expect(received).toEqual([{
      sequence: 3, event_type: "plan_created", payload: { revision: 1 }, created_at: "2026-08-02T00:00:00Z",
    }])
  })

  it("sends an empty context when no source hints are supplied", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ id: "run-1", status: "queued", summary: null, error_code: null }), {
        status: 201, headers: { "Content-Type": "application/json" },
      }),
    )
    vi.stubGlobal("fetch", fetchMock)

    await createAgentRun("t", { goal: "找岗位", allowed_skills: ["job-discovery"] })

    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      goal: "找岗位", allowed_skills: ["job-discovery"], context: {},
    })
  })

  it("ignores SSE blocks without a data line", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response("event: ping\n\n", { status: 200, headers: { "Content-Type": "text/event-stream" } }),
    ))
    const received: unknown[] = []
    await streamAgentRunEvents("t", "run-1", 0, undefined, (e) => received.push(e))
    expect(received).toEqual([])
  })

  it("ignores SSE blocks whose data is not a JSON object", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response("data: 123\n\ndata: null\n\ndata: [1,2]\n\n", {
        status: 200, headers: { "Content-Type": "text/event-stream" },
      }),
    ))
    const received: unknown[] = []
    await streamAgentRunEvents("t", "run-1", 0, undefined, (e) => received.push(e))
    expect(received).toEqual([])
  })

  it("ignores SSE blocks whose payload misses required event fields", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response('data: {"foo":"bar"}\n\n', {
        status: 200, headers: { "Content-Type": "text/event-stream" },
      }),
    ))
    const received: unknown[] = []
    await streamAgentRunEvents("t", "run-1", 0, undefined, (e) => received.push(e))
    expect(received).toEqual([])
  })

  it("ignores SSE blocks whose data line is not parseable JSON", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response("data: {not-json\n\ndata: also-broken\n\n", {
        status: 200, headers: { "Content-Type": "text/event-stream" },
      }),
    ))
    const received: unknown[] = []
    await streamAgentRunEvents("t", "run-1", 0, undefined, (e) => received.push(e))
    expect(received).toEqual([])
  })

  it("raises an API error when the event stream responds non-OK", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("", { status: 503 })))
    await expect(
      streamAgentRunEvents("t", "run-1", 0, undefined, () => {}),
    ).rejects.toMatchObject({ status: 503 })
  })

  it("raises when the event stream has no readable body", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(null, { status: 200 })))
    await expect(
      streamAgentRunEvents("t", "run-1", 0, undefined, () => {}),
    ).rejects.toThrow("事件流不可用")
  })
})
