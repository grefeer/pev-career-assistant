import { flushPromises, mount } from "@vue/test-utils"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ApiError } from "../../../api"
import AgentWorkspace from "../AgentWorkspace.vue"

const api = vi.hoisted(() => ({
  createAgentRun: vi.fn(),
  fetchAgentRun: vi.fn(),
  fetchAgentRunArtifacts: vi.fn(),
  fetchAgentRunEvents: vi.fn(),
  fetchAgentRuns: vi.fn(),
  resumeAgentRun: vi.fn(),
}))
vi.mock("../agentRuntimeApi", () => api)

const run = {
  id: "run-1",
  goal: "找 AI Agent 岗位",
  status: "succeeded",
  complexity: "L3",
  summary: "已找到岗位",
  error_code: null,
  created_at: "2026-08-02T00:00:00Z",
  updated_at: "2026-08-02T00:00:00Z",
}

beforeEach(() => {
  vi.clearAllMocks()
  api.fetchAgentRuns.mockResolvedValue({ items: [] })
  api.fetchAgentRun.mockResolvedValue(run)
  api.fetchAgentRunEvents.mockResolvedValue({ items: [] })
  api.fetchAgentRunArtifacts.mockResolvedValue({ items: [] })
  api.createAgentRun.mockResolvedValue({
    id: "run-1", status: "succeeded", summary: "已找到岗位", error_code: null,
  })
  api.resumeAgentRun.mockResolvedValue({
    id: "run-1", status: "succeeded", summary: "已按北京筛选", error_code: null,
  })
})

describe("AgentWorkspace", () => {
  it("submits a natural-language goal with the selected Skill authority", async () => {
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()

    await wrapper.get('textarea[name="goal"]').setValue("找最近三天的 AI Agent 岗位")
    await wrapper.get('textarea[name="candidate-urls"]').setValue("https://jobs.example/1\n")
    await wrapper.get("form").trigger("submit.prevent")
    await flushPromises()

    expect(api.createAgentRun).toHaveBeenCalledWith("student-token", {
      goal: "找最近三天的 AI Agent 岗位",
      allowed_skills: [
        "job-discovery",
        "job-matching",
        "resume-tailoring",
        "career-planning",
      ],
      candidate_urls: ["https://jobs.example/1"],
    })
  })

  it("renders owner-safe artifacts as source-linked evidence cards", async () => {
    api.fetchAgentRuns.mockResolvedValue({ items: [run] })
    api.fetchAgentRunEvents.mockResolvedValue({
      items: [{ sequence: 1, event_type: "verification_passed", payload: {}, created_at: run.created_at }],
    })
    api.fetchAgentRunArtifacts.mockResolvedValue({
      items: [{
        id: "artifact-1", artifact_type: "public_job_page", source_url: "https://jobs.example/1",
        content_hash: "a".repeat(64), content: { title: "AI Agent 开发工程师", visible_text: "岗位职责" },
        created_at: run.created_at,
      }],
    })

    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()

    expect(wrapper.text()).toContain("来源证据")
    expect(wrapper.text()).toContain("AI Agent 开发工程师")
    expect(wrapper.find('a[href="https://jobs.example/1"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain("private_context")
  })

  it("maps an unavailable runtime to a user-readable safe error", async () => {
    api.createAgentRun.mockRejectedValue(
      new ApiError(503, { code: "agent_harness_unavailable" }, "agent_harness_unavailable"),
    )
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()

    await wrapper.get('textarea[name="goal"]').setValue("找岗位")
    await wrapper.get("form").trigger("submit.prevent")
    await flushPromises()

    expect(wrapper.text()).toContain("智能求职助手暂不可用，请稍后重试。")
  })

  it("lets the owner provide a clarification for a waiting run", async () => {
    const waitingRun = { ...run, status: "waiting_user", summary: "请确认目标城市" }
    api.fetchAgentRuns.mockResolvedValue({ items: [waitingRun] })
    api.fetchAgentRun.mockResolvedValue(waitingRun)
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()

    await wrapper.get('textarea[name="user-response"]').setValue("北京")
    await wrapper.get('form[name="resume-run"]').trigger("submit.prevent")
    await flushPromises()

    expect(api.resumeAgentRun).toHaveBeenCalledWith("student-token", "run-1", "北京")
  })
})
