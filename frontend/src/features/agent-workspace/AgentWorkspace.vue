<script setup lang="ts">
import { computed, onMounted, ref } from "vue"

import { ApiError } from "../../api"
import {
  createAgentRun,
  fetchAgentRun,
  fetchAgentRunArtifacts,
  fetchAgentRunEvents,
  fetchAgentRuns,
  resumeAgentRun,
} from "./agentRuntimeApi"
import type {
  AgentArtifactResponse,
  AgentEventResponse,
  AgentRunResponse,
  CareerSkillName,
} from "./agentRuntimeTypes"

const props = defineProps<{ token?: string | null }>()

const skills: Array<{ name: CareerSkillName; label: string; detail: string }> = [
  { name: "job-discovery", label: "岗位发现", detail: "抓取、提取与证据化 JD" },
  { name: "job-matching", label: "岗位匹配", detail: "基于已确认事实排序" },
  { name: "resume-tailoring", label: "简历定制", detail: "只给出有事实依据的修改" },
  { name: "career-planning", label: "求职计划", detail: "给出面试与行动建议" },
]

const goal = ref("")
const candidateUrlsText = ref("")
const selectedSkills = ref<CareerSkillName[]>(skills.map((skill) => skill.name))
const runs = ref<AgentRunResponse[]>([])
const activeRun = ref<AgentRunResponse | null>(null)
const events = ref<AgentEventResponse[]>([])
const artifacts = ref<AgentArtifactResponse[]>([])
const loadingHistory = ref(false)
const loadingRun = ref(false)
const submitting = ref(false)
const resuming = ref(false)
const errorMessage = ref<string | null>(null)
const userResponse = ref("")

const canSubmit = computed(() => Boolean(
  props.token && goal.value.trim() && selectedSkills.value.length && !submitting.value,
))

onMounted(async () => {
  await loadHistory()
})

function candidateUrls(): string[] {
  return candidateUrlsText.value
    .split(/\r?\n/)
    .map((url) => url.trim())
    .filter(Boolean)
}

async function loadHistory(): Promise<void> {
  if (!props.token) return
  loadingHistory.value = true
  try {
    const response = await fetchAgentRuns(props.token, 20)
    runs.value = response.items
    if (!activeRun.value && response.items[0]) {
      await selectRun(response.items[0].id)
    }
  } catch (error: unknown) {
    errorMessage.value = userFacingError(error)
  } finally {
    loadingHistory.value = false
  }
}

async function selectRun(runId: string): Promise<void> {
  if (!props.token) return
  loadingRun.value = true
  errorMessage.value = null
  try {
    const [run, eventResponse, artifactResponse] = await Promise.all([
      fetchAgentRun(props.token, runId),
      fetchAgentRunEvents(props.token, runId),
      fetchAgentRunArtifacts(props.token, runId),
    ])
    activeRun.value = run
    events.value = eventResponse.items
    artifacts.value = artifactResponse.items
  } catch (error: unknown) {
    errorMessage.value = userFacingError(error)
  } finally {
    loadingRun.value = false
  }
}

async function submit(): Promise<void> {
  if (!canSubmit.value || !props.token) return
  submitting.value = true
  errorMessage.value = null
  try {
    const result = await createAgentRun(props.token, {
      goal: goal.value.trim(),
      allowed_skills: [...selectedSkills.value],
      candidate_urls: candidateUrls(),
    })
    await loadHistory()
    await selectRun(result.id)
  } catch (error: unknown) {
    errorMessage.value = userFacingError(error)
  } finally {
    submitting.value = false
  }
}

async function resumeActiveRun(): Promise<void> {
  if (!props.token || !activeRun.value || !userResponse.value.trim() || resuming.value) return
  resuming.value = true
  errorMessage.value = null
  try {
    await resumeAgentRun(props.token, activeRun.value.id, userResponse.value)
    userResponse.value = ""
    await loadHistory()
    await selectRun(activeRun.value.id)
  } catch (error: unknown) {
    errorMessage.value = userFacingError(error)
  } finally {
    resuming.value = false
  }
}

function userFacingError(error: unknown): string {
  if (error instanceof ApiError && (
    error.message === "agent_harness_unavailable"
    || error.message === "agent_harness_disabled"
  )) {
    return "智能求职助手暂不可用，请稍后重试。"
  }
  if (error instanceof Error && error.message.trim()) return error.message
  return "任务加载失败，请稍后重试。"
}

function eventLabel(event: AgentEventResponse): string {
  const labels: Record<string, string> = {
    run_started: "任务已启动",
    run_resumed: "已收到你的补充，任务继续执行",
    planner_needs_user: "Planner 需要补充信息",
    run_needs_user: "任务等待你的补充",
    plan_created: "Planner 已生成计划",
    executor_tool_observation: "Executor 获取了岗位证据",
    executor_structured_artifact: "Executor 已结构化 JD",
    executor_tool_failed: "Executor 工具调用未成功",
    verification_passed: "Verifier 已通过核验",
    verification_retry_executor: "Verifier 请求补充执行",
    verification_replan: "Verifier 请求重新规划",
    step_succeeded: "计划步骤完成",
    run_succeeded: "任务完成",
    run_failed: "任务未完成",
  }
  return labels[event.event_type] ?? event.event_type
}

function artifactTitle(artifact: AgentArtifactResponse): string {
  const title = artifact.content.title
  if (typeof title === "string" && title) return title
  const candidates = artifact.content.candidates
  if (Array.isArray(candidates) && candidates[0] && typeof candidates[0] === "object") {
    const candidateTitle = (candidates[0] as Record<string, unknown>).title
    if (typeof candidateTitle === "string" && candidateTitle) return candidateTitle
  }
  return artifact.artifact_type === "structured_job_details" ? "结构化 JD" : "公开岗位页面"
}

function artifactDetail(artifact: AgentArtifactResponse): string {
  const visibleText = artifact.content.visible_text
  if (typeof visibleText === "string" && visibleText) return visibleText.slice(0, 180)
  const candidates = artifact.content.candidates
  if (Array.isArray(candidates)) return `已提取 ${candidates.length} 个结构化岗位条目。`
  return "该工件没有可展示的文本摘要。"
}

function formatDate(value: string): string {
  return new Date(value).toLocaleString("zh-CN", { dateStyle: "medium", timeStyle: "short" })
}
</script>

<template>
  <div class="agent-workspace">
    <section class="masthead">
      <p class="kicker">PERSONAL CAREER DESK</p>
      <h1>把求职目标，变成一条可核验的行动路径。</h1>
      <p class="masthead-copy">Planner 负责拆解，Executor 调用已授权的求职技能，Verifier 在复杂任务中检查来源和结果。</p>
    </section>

    <section class="task-composer" aria-labelledby="task-heading">
      <div class="section-heading">
        <p class="section-index">01 / 发起任务</p>
        <h2 id="task-heading">今天想推进什么？</h2>
      </div>
      <form @submit.prevent="submit">
        <label class="field-label" for="goal">自然语言目标</label>
        <textarea id="goal" v-model="goal" name="goal" rows="4" placeholder="例如：统计最近三天北京和上海的 AI 应用开发、Agent 开发岗位，推荐最适合我的岗位并给出简历和面试建议。" />

        <div class="skill-grid" aria-label="允许调用的求职技能">
          <label v-for="skill in skills" :key="skill.name" class="skill-toggle">
            <input v-model="selectedSkills" type="checkbox" :value="skill.name" />
            <span>
              <strong>{{ skill.label }}</strong>
              <small>{{ skill.detail }}</small>
            </span>
          </label>
        </div>

        <label class="field-label" for="candidate-urls">公开岗位 URL（可选，每行一个）</label>
        <textarea id="candidate-urls" v-model="candidateUrlsText" name="candidate-urls" rows="3" placeholder="仅粘贴公开招聘页面；系统不会绕过登录、验证码或反爬限制。" />
        <div class="composer-footer">
          <p>运行预算由服务端固定；助手不会自动投递，也不会把没有事实依据的内容写进简历。</p>
          <button type="submit" :disabled="!canSubmit">{{ submitting ? "任务执行中…" : "开始求职任务" }}</button>
        </div>
      </form>
    </section>

    <p v-if="errorMessage" class="error-banner" role="alert">{{ errorMessage }}</p>

    <section class="workspace-grid" aria-live="polite">
      <aside class="run-history" aria-label="最近任务">
        <div class="section-heading compact">
          <p class="section-index">02 / 任务历史</p>
          <h2>近期工作</h2>
        </div>
        <p v-if="loadingHistory" class="muted">正在读取任务…</p>
        <p v-else-if="!runs.length" class="muted">还没有任务。从上方描述你的目标开始。</p>
        <button
          v-for="run in runs"
          :key="run.id"
          class="run-button"
          :class="{ active: activeRun?.id === run.id }"
          type="button"
          @click="selectRun(run.id)"
        >
          <span class="status-dot" :class="run.status" />
          <span><strong>{{ run.goal }}</strong><small>{{ run.complexity ?? "待规划" }} · {{ formatDate(run.created_at) }}</small></span>
        </button>
      </aside>

      <section class="run-detail" aria-labelledby="detail-heading">
        <div v-if="loadingRun" class="empty-detail">正在装配任务证据…</div>
        <div v-else-if="!activeRun" class="empty-detail">选择一项任务，查看计划、活动和工件。</div>
        <template v-else>
          <header class="detail-header">
            <p class="section-index">03 / 当前任务</p>
            <h2 id="detail-heading">{{ activeRun.goal }}</h2>
            <p>{{ activeRun.summary ?? "任务仍在处理，完成后将在这里显示安全摘要。" }}</p>
            <span class="status-pill" :class="activeRun.status">{{ activeRun.status }}</span>
            <form
              v-if="activeRun.status === 'waiting_user'"
              name="resume-run"
              class="resume-form"
              @submit.prevent="resumeActiveRun"
            >
              <label class="field-label" for="user-response">补充信息后继续</label>
              <textarea id="user-response" v-model="userResponse" name="user-response" rows="2" placeholder="例如：优先北京，接受上海；只看正式岗位。" />
              <button type="submit" :disabled="resuming || !userResponse.trim()">{{ resuming ? "正在继续…" : "继续任务" }}</button>
            </form>
          </header>

          <div class="detail-columns">
            <section class="activity-panel">
              <h3>Agent 活动</h3>
              <ol v-if="events.length" class="timeline">
                <li v-for="event in events" :key="event.sequence">
                  <span class="timeline-number">{{ String(event.sequence).padStart(2, "0") }}</span>
                  <span><strong>{{ eventLabel(event) }}</strong><small>{{ formatDate(event.created_at) }}</small></span>
                </li>
              </ol>
              <p v-else class="muted">该任务尚未产生可展示的活动事件。</p>
            </section>

            <section class="artifact-panel">
              <h3>来源证据与工件</h3>
              <article v-for="artifact in artifacts" :key="artifact.id" class="artifact-card">
                <p class="artifact-type">{{ artifact.artifact_type }}</p>
                <h4>{{ artifactTitle(artifact) }}</h4>
                <p>{{ artifactDetail(artifact) }}</p>
                <a :href="artifact.source_url" target="_blank" rel="noreferrer">查看来源证据 ↗</a>
              </article>
              <p v-if="!artifacts.length" class="muted">完成后，岗位证据、结构化 JD 和建议工件会显示在这里。</p>
            </section>
          </div>
        </template>
      </section>
    </section>
  </div>
</template>

<style scoped>
.agent-workspace { max-width: 1440px; margin: 0 auto; color: #24221d; }
.masthead { max-width: 850px; padding: 2.5rem 0 2.1rem; }
.kicker, .section-index, .artifact-type { margin: 0; color: #a44b23; font: 700 .72rem/1.2 Georgia, serif; letter-spacing: .12em; text-transform: uppercase; }
h1, h2, h3, h4, p { margin-top: 0; }
h1 { margin-bottom: .9rem; font: 700 clamp(2.2rem, 5vw, 4.4rem)/.96 Georgia, "Songti SC", serif; letter-spacing: -.045em; }
.masthead-copy { max-width: 650px; color: #625d52; font-size: 1.05rem; }
.task-composer, .run-history, .run-detail { border: 1px solid #d9d1c2; background: rgba(255, 253, 248, .82); box-shadow: 0 18px 45px rgba(67, 50, 25, .08); }
.task-composer { padding: 1.5rem; border-top: 4px solid #a44b23; }
.section-heading h2 { margin: .25rem 0 1rem; font: 700 1.6rem/1 Georgia, "Songti SC", serif; }
.field-label { display: block; margin: 1rem 0 .45rem; color: #524d43; font-weight: 700; }
textarea { width: 100%; padding: .8rem; border: 1px solid #cfc4b2; border-radius: 2px; background: #fffdf8; color: #29251e; line-height: 1.5; resize: vertical; }
textarea:focus { outline: 3px solid rgba(164, 75, 35, .2); border-color: #a44b23; }
.skill-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: .65rem; margin-top: 1.1rem; }
.skill-toggle { display: flex; gap: .55rem; min-height: 74px; padding: .75rem; border: 1px solid #d9d1c2; background: #f5efe4; cursor: pointer; }
.skill-toggle:has(input:checked) { border-color: #a44b23; background: #f7e4d6; }
.skill-toggle input { accent-color: #a44b23; margin-top: .18rem; }
.skill-toggle strong, .skill-toggle small { display: block; }
.skill-toggle small { margin-top: .2rem; color: #6c6559; font-size: .76rem; }
.composer-footer { display: flex; justify-content: space-between; gap: 1rem; align-items: center; margin-top: 1rem; }
.composer-footer p { max-width: 690px; margin: 0; color: #696155; font-size: .82rem; }
button { border: 0; cursor: pointer; }
.composer-footer button { flex: 0 0 auto; padding: .8rem 1rem; background: #24221d; color: #fffaf0; font-weight: 700; }
.composer-footer button:disabled { cursor: not-allowed; opacity: .45; }
.error-banner { margin: 1rem 0; padding: .8rem 1rem; border-left: 4px solid #b42318; background: #fce9e7; color: #7a1c17; }
.workspace-grid { display: grid; grid-template-columns: 285px minmax(0, 1fr); gap: 1rem; margin-top: 1rem; }
.run-history { align-self: start; padding: 1.1rem; }
.compact h2 { font-size: 1.25rem; }
.run-button { display: flex; width: 100%; gap: .55rem; padding: .8rem 0; border-bottom: 1px solid #e4dccf; background: transparent; text-align: left; color: inherit; }
.run-button:last-child { border-bottom: 0; }
.run-button.active strong { color: #a44b23; }
.run-button strong, .run-button small { display: block; }
.run-button strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font: 700 .9rem/1.3 Georgia, serif; }
.run-button small { margin-top: .25rem; color: #766f63; font-size: .7rem; }
.status-dot { flex: 0 0 8px; height: 8px; margin-top: .25rem; border-radius: 50%; background: #b5aea3; }
.status-dot.succeeded { background: #39735b; }.status-dot.failed { background: #b42318; }.status-dot.waiting_user { background: #ba7a18; }.status-dot.running { background: #2f6f96; }
.run-detail { min-height: 380px; padding: 1.4rem; }
.detail-header { position: relative; padding-right: 100px; border-bottom: 1px solid #dfd6c7; }
.detail-header h2 { margin: .25rem 0 .7rem; font: 700 clamp(1.55rem, 3vw, 2.4rem)/1 Georgia, "Songti SC", serif; }.detail-header > p:not(.section-index) { color: #625d52; }
.status-pill { position: absolute; top: .2rem; right: 0; padding: .3rem .55rem; background: #e9e3d8; color: #514c42; font: 700 .7rem/1 monospace; text-transform: uppercase; }.status-pill.succeeded { background: #dbece0; color: #24533d; }.status-pill.failed { background: #f7dedd; color: #84221d; }
.resume-form { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: .55rem .75rem; align-items: end; margin: 1rem 0; padding: .85rem; border: 1px solid #dcc07b; background: #fff7dc; }.resume-form .field-label { grid-column: 1 / -1; margin: 0; }.resume-form textarea { min-height: 70px; }.resume-form button { align-self: stretch; padding: .7rem .85rem; background: #8b5a16; color: #fffaf0; font-weight: 700; }.resume-form button:disabled { cursor: not-allowed; opacity: .45; }
.detail-columns { display: grid; grid-template-columns: minmax(210px, .8fr) minmax(260px, 1.2fr); gap: 1.5rem; padding-top: 1.2rem; }.detail-columns h3 { margin-bottom: .8rem; font: 700 1.12rem/1 Georgia, serif; }
.timeline { margin: 0; padding: 0; list-style: none; }.timeline li { display: flex; gap: .65rem; padding: .65rem 0; border-bottom: 1px solid #eee7db; }.timeline-number { color: #a44b23; font: 700 .75rem/1.4 monospace; }.timeline strong, .timeline small { display: block; }.timeline strong { font-size: .88rem; }.timeline small { color: #80786d; font-size: .72rem; }
.artifact-card { margin-bottom: .75rem; padding: .9rem; border: 1px solid #ded3c0; background: #fbf6ed; }.artifact-card h4 { margin: .25rem 0 .45rem; font: 700 1rem/1.1 Georgia, serif; }.artifact-card > p:not(.artifact-type) { margin-bottom: .6rem; color: #5f584d; font-size: .83rem; }.artifact-card a { color: #8d3c1c; font-size: .82rem; font-weight: 700; }
.muted, .empty-detail { color: #756d61; }.empty-detail { display: grid; min-height: 330px; place-items: center; font: 1.1rem Georgia, serif; text-align: center; }
@media (max-width: 900px) { .skill-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }.workspace-grid, .detail-columns { grid-template-columns: 1fr; }.run-history { max-height: 260px; overflow: auto; }.composer-footer { align-items: flex-start; flex-direction: column; }.detail-header { padding-right: 0; }.status-pill { position: static; display: inline-block; margin-bottom: .75rem; } }
@media (max-width: 520px) { .skill-grid { grid-template-columns: 1fr; }.masthead { padding-top: 1rem; }.task-composer, .run-detail, .run-history { padding: 1rem; } }
</style>
