import { flushPromises, mount } from "@vue/test-utils"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { ApiError } from "../../../api"
import AgentWorkspace from "../AgentWorkspace.vue"

const api = vi.hoisted(() => ({
  createAgentRun: vi.fn(),
  fetchAgentRun: vi.fn(),
  fetchAgentRunArtifacts: vi.fn(),
  fetchAgentRunEvents: vi.fn(),
  fetchAgentRunPlans: vi.fn(),
  recoverAgentRun: vi.fn(),
  fetchAgentRuns: vi.fn(),
  resumeAgentRun: vi.fn(),
  streamAgentRunEvents: vi.fn(),
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
  api.fetchAgentRunPlans.mockResolvedValue({ items: [] })
  api.fetchAgentRunArtifacts.mockResolvedValue({ items: [] })
  api.createAgentRun.mockResolvedValue({
    id: "run-1", status: "succeeded", summary: "已找到岗位", error_code: null,
  })
  api.resumeAgentRun.mockResolvedValue({
    id: "run-1", status: "succeeded", summary: "已按北京筛选", error_code: null,
  })
  api.recoverAgentRun.mockResolvedValue({
    id: "run-1", status: "succeeded", summary: "恢复完成", error_code: null,
  })
  api.streamAgentRunEvents.mockResolvedValue(undefined)
})

describe("AgentWorkspace", () => {
  it("degrades to an empty signed-out workspace without attempting private history", async () => {
    const wrapper = mount(AgentWorkspace)
    await flushPromises()

    expect(api.fetchAgentRuns).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain("还没有任务")
  })

  it("requires both an authenticated token and at least one explicit Skill authority", async () => {
    const signedOut = mount(AgentWorkspace)
    await signedOut.get('textarea[name="goal"]').setValue("找岗位")
    await signedOut.get("form").trigger("submit.prevent")
    expect(api.createAgentRun).not.toHaveBeenCalled()

    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()
    await wrapper.get('textarea[name="goal"]').setValue("找岗位")
    for (const checkbox of wrapper.findAll('input[type="checkbox"]')) {
      await checkbox.setValue(false)
    }
    expect(wrapper.get(".composer-footer button").attributes("disabled")).toBeDefined()
    await wrapper.get("form").trigger("submit.prevent")
    expect(api.createAgentRun).not.toHaveBeenCalled()
  })

  it("surfaces history and run-detail loading failures without leaking technical context", async () => {
    api.fetchAgentRuns.mockRejectedValueOnce({})
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()
    expect(wrapper.text()).toContain("任务加载失败，请稍后重试。")

    api.fetchAgentRuns.mockResolvedValue({ items: [run, { ...run, id: "run-2", goal: "另一个任务" }] })
    api.fetchAgentRun.mockResolvedValueOnce(run).mockRejectedValueOnce(new Error("详情不可用"))
    const detailWrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()
    await detailWrapper.findAll(".run-button")[1].trigger("click")
    await flushPromises()
    expect(detailWrapper.text()).toContain("详情不可用")
  })

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

  it("subscribes from the durable event cursor and renders live owner-safe progress", async () => {
    api.fetchAgentRuns.mockResolvedValue({ items: [run] })
    api.streamAgentRunEvents.mockImplementation(
      (_token: string, _runId: string, _cursor: number, _signal: AbortSignal, onEvent: (event: unknown) => void) => {
        onEvent({ sequence: 2, event_type: "plan_created", payload: { revision: 1 }, created_at: run.created_at })
        return Promise.resolve()
      },
    )
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()

    expect(api.streamAgentRunEvents).toHaveBeenCalledWith(
      "student-token", "run-1", 0, expect.any(AbortSignal), expect.any(Function),
    )
    expect(wrapper.text()).toContain("Planner 已生成计划")
  })

  it("renders the safe Planner step projection without task-private context", async () => {
    api.fetchAgentRuns.mockResolvedValue({ items: [run] })
    api.fetchAgentRunPlans.mockResolvedValue({
      items: [{
        id: "plan-1", revision: 1, complexity: "L3", success_criteria: ["输出可核验匹配"],
        steps: [{
          id: "match", objective: "基于证据匹配", allowed_skills: ["job-matching"],
          success_criteria: ["给出理由"], requires_verification: true,
        }], created_at: run.created_at,
      }],
    })
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()

    expect(wrapper.text()).toContain("Planner 计划")
    expect(wrapper.text()).toContain("基于证据匹配")
    expect(wrapper.text()).toContain("需完成门禁核验")
    expect(wrapper.text()).not.toContain("private_context")
  })

  it("renders a persisted grounded resume diff as a reviewable artifact", async () => {
    api.fetchAgentRuns.mockResolvedValue({ items: [run] })
    api.fetchAgentRunArtifacts.mockResolvedValue({
      items: [{
        id: "artifact-resume", artifact_type: "resume_tailoring_brief",
        source_url: "https://jobs.example/1", content_hash: "b".repeat(64),
        content: {
          proposed_diffs: [{
            section: "skills", fact_ref: "skills", target_evidence_ref: "jd-1",
            change_summary: "将已确认的 Agent 事实前置到技能部分。",
          }],
        }, created_at: run.created_at,
      }],
    })
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()

    expect(wrapper.text()).toContain("简历定制修改建议")
    expect(wrapper.text()).toContain("1 条可审核的简历修改操作")
    expect(wrapper.text()).toContain("将已确认的 Agent 事实前置到技能部分。")
    expect(wrapper.text()).toContain("事实字段：skills")
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

  it("shows a normal create failure and respects an empty response to a waiting run", async () => {
    api.createAgentRun.mockRejectedValue(new Error("创建失败"))
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()
    await wrapper.get('textarea[name="goal"]').setValue("找岗位")
    await wrapper.get("form").trigger("submit.prevent")
    await flushPromises()
    expect(wrapper.text()).toContain("创建失败")

    const waitingRun = { ...run, status: "waiting_user" }
    api.fetchAgentRuns.mockResolvedValue({ items: [waitingRun] })
    api.fetchAgentRun.mockResolvedValue(waitingRun)
    const waitingWrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()
    await waitingWrapper.get('form[name="resume-run"]').trigger("submit.prevent")
    expect(api.resumeAgentRun).not.toHaveBeenCalled()
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

  it("lets the owner recover an interrupted running run without client context", async () => {
    const interruptedRun = { ...run, status: "running", summary: null }
    api.fetchAgentRuns.mockResolvedValue({ items: [interruptedRun] })
    api.fetchAgentRun.mockResolvedValue(interruptedRun)
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()

    await wrapper.get('button[name="recover-run"]').trigger("click")
    await flushPromises()

    expect(api.recoverAgentRun).toHaveBeenCalledWith("student-token", "run-1")
  })

  it("shows resume and recovery errors while preserving the active run", async () => {
    const waitingRun = { ...run, status: "waiting_user" }
    api.fetchAgentRuns.mockResolvedValue({ items: [waitingRun] })
    api.fetchAgentRun.mockResolvedValue(waitingRun)
    api.resumeAgentRun.mockRejectedValue(new Error("无法继续"))
    const waitingWrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()
    await waitingWrapper.get('textarea[name="user-response"]').setValue("北京")
    await waitingWrapper.get('form[name="resume-run"]').trigger("submit.prevent")
    await flushPromises()
    expect(waitingWrapper.text()).toContain("无法继续")

    const runningRun = { ...run, status: "running" }
    api.fetchAgentRuns.mockResolvedValue({ items: [runningRun] })
    api.fetchAgentRun.mockResolvedValue(runningRun)
    api.recoverAgentRun.mockRejectedValue(new Error("恢复失败"))
    const runningWrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()
    await runningWrapper.get('button[name="recover-run"]').trigger("click")
    await flushPromises()
    expect(runningWrapper.text()).toContain("恢复失败")
  })

  it("renders unknown events and safe artifact fallbacks while discarding malformed previews", async () => {
    api.fetchAgentRuns.mockResolvedValue({ items: [run] })
    api.fetchAgentRunEvents.mockResolvedValue({
      items: [{ sequence: 2, event_type: "future_event", payload: {}, created_at: run.created_at }],
    })
    api.fetchAgentRunArtifacts.mockResolvedValue({
      items: [
        {
          id: "candidate-title", artifact_type: "structured_job_details", source_url: "https://jobs.example/2",
          content_hash: "d".repeat(64), content: { candidates: [{ title: "候选岗位" }] }, created_at: run.created_at,
        },
        {
          id: "topics", artifact_type: "public_job_page", source_url: "https://jobs.example/3",
          content_hash: "e".repeat(64), content: { jd_topics: ["Agent", "RAG"] }, created_at: run.created_at,
        },
        {
          id: "fallback", artifact_type: "job_matching_report", source_url: "https://jobs.example/4",
          content_hash: "f".repeat(64), content: {}, created_at: run.created_at,
        },
        {
          id: "malformed", artifact_type: "resume_tailoring_brief", source_url: "https://jobs.example/5",
          content_hash: "g".repeat(64), content: { proposed_diffs: [null, { section: "skills" }] }, created_at: run.created_at,
        },
      ],
    })
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()

    expect(wrapper.text()).toContain("future_event")
    expect(wrapper.text()).toContain("候选岗位")
    expect(wrapper.text()).toContain("已提取 1 个结构化岗位条目")
    expect(wrapper.text()).toContain("岗位匹配报告")
    expect(wrapper.text()).toContain("该工件没有可展示的文本摘要")
    expect(wrapper.find('[aria-label="可审核的简历修改"]').exists()).toBe(false)
  })

  it("aborts selectRun and startEventStream when the token is cleared mid-session", async () => {
    api.fetchAgentRuns.mockResolvedValue({ items: [run] })
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()
    // mount auto-selected run-1 with the token present (guards' false arms)
    expect(api.fetchAgentRun).toHaveBeenCalledWith("student-token", "run-1")

    // clearing the token mid-session: the UI never re-invokes these, so exercise
    // the token-absent guards through the exposed instance.
    await wrapper.setProps({ token: undefined })
    await wrapper.vm.selectRun("run-1")
    wrapper.vm.startEventStream("run-1", 0)
    await flushPromises()

    // neither path issued a fetch/stream for the now-absent token
    expect(api.fetchAgentRun).not.toHaveBeenCalledWith(undefined, "run-1")
    expect(api.streamAgentRunEvents).toHaveBeenCalledTimes(1)
  })

  it("skips recovery when there is no active running run", async () => {
    api.fetchAgentRuns.mockResolvedValue({ items: [] })
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()
    // no runs -> activeRun stays null -> the recover button is not rendered,
    // so the entry guard is exercised through the exposed instance.
    await wrapper.vm.recoverActiveRun()
    expect(api.recoverAgentRun).not.toHaveBeenCalled()
  })

  it("surfaces a non-harness API error with its own message", async () => {
    api.createAgentRun.mockRejectedValue(new ApiError(500, { code: "internal" }, "服务内部错误"))
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()
    await wrapper.get('textarea[name="goal"]').setValue("找岗位")
    await wrapper.get("form").trigger("submit.prevent")
    await flushPromises()
    // ApiError whose message is neither harness_unavailable nor harness_disabled
    expect(wrapper.text()).toContain("服务内部错误")
    expect(wrapper.text()).not.toContain("智能求职助手暂不可用")
  })

  it("falls back to the artifact-type label when a candidate has an empty title", async () => {
    api.fetchAgentRuns.mockResolvedValue({ items: [run] })
    api.fetchAgentRunArtifacts.mockResolvedValue({
      items: [{
        id: "empty-title", artifact_type: "structured_job_details", source_url: "https://jobs.example/7",
        content_hash: "1".repeat(64), content: { candidates: [{ title: "" }] }, created_at: run.created_at,
      }],
    })
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()
    // candidateTitle === "" (string but falsy) -> falls through to the titles map
    expect(wrapper.text()).toContain("结构化 JD")
  })

  it("shows a placeholder complexity when a run has no complexity", async () => {
    api.fetchAgentRuns.mockResolvedValue({ items: [{ ...run, complexity: null as unknown as string }] })
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()
    // run.complexity ?? "待规划" -> null -> "待规划"
    expect(wrapper.text()).toContain("待规划")
  })

  it("labels deterministic plan steps distinctly from verified ones", async () => {
    api.fetchAgentRuns.mockResolvedValue({ items: [run] })
    api.fetchAgentRunPlans.mockResolvedValue({
      items: [{
        id: "plan-1", revision: 1, complexity: "L3", success_criteria: [],
        steps: [{
          id: "fetch", objective: "抓取公开页面", allowed_skills: ["job-discovery"],
          success_criteria: [], requires_verification: false,
        }], created_at: run.created_at,
      }],
    })
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()
    // step.requires_verification ? ... : "确定性校验" -> false
    expect(wrapper.text()).toContain("确定性校验")
  })

  it("drops stale stream events and orders live events by sequence", async () => {
    api.fetchAgentRuns.mockResolvedValue({ items: [run, { ...run, id: "run-2", goal: "第二任务" }] })
    api.fetchAgentRun.mockImplementation((_t: string, id: string) =>
      Promise.resolve(id === "run-2" ? { ...run, id: "run-2", goal: "第二任务" } : run))
    const streams: Array<{ runId: string; onEvent: (event: unknown) => void }> = []
    api.streamAgentRunEvents.mockImplementation(
      (_t: string, runId: string, _cursor: number, _signal: AbortSignal, onEvent: (event: unknown) => void) => {
        streams.push({ runId, onEvent })
        return Promise.resolve()
      },
    )
    // seed one event so a live callback sorts >= 2 entries (covers the comparator)
    api.fetchAgentRunEvents.mockResolvedValue({
      items: [{ sequence: 1, event_type: "run_started", payload: {}, created_at: run.created_at }],
    })

    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()
    const run1Stream = streams[0]
    expect(run1Stream.runId).toBe("run-1")

    // live event for the active run: keeps seq 1 + appends seq 3 -> comparator runs
    run1Stream.onEvent({ sequence: 3, event_type: "plan_created", payload: {}, created_at: run.created_at })
    await flushPromises()
    expect(wrapper.text()).toContain("Planner 已生成计划")

    // switch active run to run-2 -> the run-1 stream becomes stale
    await wrapper.findAll(".run-button")[1].trigger("click")
    await flushPromises()
    // a late event on the stale run-1 stream is dropped (activeRun.id !== runId -> return)
    run1Stream.onEvent({ sequence: 99, event_type: "run_failed", payload: {}, created_at: run.created_at })
    expect(wrapper.text()).not.toContain("任务未完成")
  })

  it("surfaces a non-aborted event-stream failure", async () => {
    api.fetchAgentRuns.mockResolvedValue({ items: [run] })
    api.streamAgentRunEvents.mockRejectedValue(new Error("事件流中断"))
    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()
    // mount auto-selects run-1 -> stream rejects while controller is NOT aborted
    expect(wrapper.text()).toContain("事件流中断")
  })

  it("silently drops an event-stream failure that arrives after the run was switched away", async () => {
    api.fetchAgentRuns.mockResolvedValue({ items: [run, { ...run, id: "run-2", goal: "第二任务" }] })
    api.fetchAgentRun.mockImplementation((_t: string, id: string) =>
      Promise.resolve(id === "run-2" ? { ...run, id: "run-2", goal: "第二任务" } : run))
    const rejectors: Array<(error: unknown) => void> = []
    api.streamAgentRunEvents.mockImplementation(
      () => new Promise<void>((_resolve, reject) => {
        rejectors.push(reject)
      }),
    )

    const wrapper = mount(AgentWorkspace, { props: { token: "student-token" } })
    await flushPromises()
    expect(rejectors.length).toBe(1)

    // switch to run-2 -> run-1 controller is aborted; run-2 stream is pending
    await wrapper.findAll(".run-button")[1].trigger("click")
    await flushPromises()
    expect(rejectors.length).toBe(2)

    // the stale run-1 stream now rejects; its controller is aborted -> silent return
    rejectors[0](new Error("run-1 stream died"))
    await flushPromises()
    expect(wrapper.text()).not.toContain("run-1 stream died")
  })
})
