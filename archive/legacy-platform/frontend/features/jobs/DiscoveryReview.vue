<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";

import { ApiError } from "../../api";
import {
  approveJobDiscoveryCandidate,
  fetchJobDiscoveryGroups,
  fetchJobDiscoveryTasks,
  rejectJobDiscoveryCandidate,
  retryJobDiscoveryTask,
} from "./jobsApi";
import type {
  DiscoveredJobCandidate,
  JobDiscoveryReviewGroup,
  JobDiscoveryTask,
} from "./jobTypes";

const props = defineProps<{ token: string }>();

const PAGE_SIZE = 10;

const tab = ref<"tasks" | "groups">("tasks");
const tasks = ref<JobDiscoveryTask[]>([]);
const groups = ref<JobDiscoveryReviewGroup[]>([]);
const tasksLoading = ref(false);
const groupsLoading = ref(false);
const error = ref("");
const message = ref("");
const actionBusy = ref<Record<string, boolean>>({});
const tasksOffset = ref(0);

function resetFeedback(): void {
  message.value = "";
  error.value = "";
}

function apiErrorMessage(caught: unknown): string {
  if (!(caught instanceof ApiError)) return "操作失败，请稍后重试。";
  if (caught.status === 401) return "登录状态已失效，请重新登录。";
  if (caught.status === 403) return "当前账号没有管理权限。";
  if (caught.detail && typeof caught.detail === "string") return caught.detail;
  return "操作失败，请稍后重试。";
}

function computeTaskStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "运行中",
    partial_success: "部分成功",
    succeeded: "已完成",
    needs_manual_review: "待人工审核",
    failed: "失败",
    cancelled: "已取消",
  };
  return labels[status] || status;
}

function computeCandidateStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    pending_review: "待审核",
    approved: "已通过",
    rejected: "已拒绝",
    merged: "已合并",
    needs_manual_review: "待人工审核",
  };
  return labels[status] || status;
}

async function loadTasks(): Promise<void> {
  tasksLoading.value = true;
  error.value = "";
  try {
    const response = await fetchJobDiscoveryTasks(props.token);
    tasks.value = response.tasks;
  } catch (caught) {
    error.value = apiErrorMessage(caught);
  } finally {
    tasksLoading.value = false;
  }
}

async function loadGroups(): Promise<void> {
  groupsLoading.value = true;
  error.value = "";
  try {
    groups.value = await fetchJobDiscoveryGroups(props.token);
  } catch (caught) {
    error.value = apiErrorMessage(caught);
  } finally {
    groupsLoading.value = false;
  }
}

function switchTab(next: "tasks" | "groups"): void {
  if (next === tab.value) return;
  tab.value = next;
  resetFeedback();
  if (next === "tasks") void loadTasks();
  else void loadGroups();
}

async function doRetry(taskId: string): Promise<void> {
  resetFeedback();
  actionBusy.value = { ...actionBusy.value, [taskId]: true };
  try {
    await retryJobDiscoveryTask(props.token, taskId);
    message.value = "任务已重新排队。";
    await loadTasks();
  } catch (caught) {
    error.value = apiErrorMessage(caught);
  } finally {
    actionBusy.value = { ...actionBusy.value, [taskId]: false };
  }
}

async function doApprove(candidate: DiscoveredJobCandidate): Promise<void> {
  resetFeedback();
  const key = `approve:${candidate.id}`;
  actionBusy.value = { ...actionBusy.value, [key]: true };
  try {
    await approveJobDiscoveryCandidate(props.token, candidate.id);
    message.value = `已通过：${candidate.company_name || candidate.title || candidate.id}`;
    await loadGroups();
  } catch (caught) {
    error.value = apiErrorMessage(caught);
  } finally {
    actionBusy.value = { ...actionBusy.value, [key]: false };
  }
}

async function doReject(candidate: DiscoveredJobCandidate): Promise<void> {
  resetFeedback();
  const key = `reject:${candidate.id}`;
  actionBusy.value = { ...actionBusy.value, [key]: true };
  try {
    await rejectJobDiscoveryCandidate(props.token, candidate.id);
    message.value = `已拒绝：${candidate.company_name || candidate.title || candidate.id}`;
    await loadGroups();
  } catch (caught) {
    error.value = apiErrorMessage(caught);
  } finally {
    actionBusy.value = { ...actionBusy.value, [key]: false };
  }
}

function isRunning(status: string): boolean {
  return status === "running";
}

const sortedTasks = ref<JobDiscoveryTask[]>([]);

const visibleTasks = ref<JobDiscoveryTask[]>([]);

onMounted(() => {
  void loadTasks();
});
</script>

<template>
  <section class="discovery-review" aria-labelledby="discovery-title">
    <header class="review-header">
      <div>
        <p class="kicker">ADMIN · JOB DISCOVERY</p>
        <h1 id="discovery-title">职位发现审核台</h1>
        <p>查看 Agent 发现结果，审核候选职位并形成正式记录。</p>
      </div>
      <div class="mode-switch">
        <button
          type="button"
          :class="{ active: tab === 'tasks' }"
          :disabled="groupsLoading"
          @click="switchTab('tasks')"
        >发现记录</button>
        <button
          type="button"
          :class="{ active: tab === 'groups' }"
          :disabled="tasksLoading"
          @click="switchTab('groups')"
        >审核分组</button>
      </div>
    </header>

    <p v-if="error" class="notice error" role="alert">{{ error }}</p>
    <p v-if="message" class="notice success" role="status">{{ message }}</p>

    <!-- Tasks Tab -->
    <div v-if="tab === 'tasks'">
      <div class="toolbar">
        <span>共 {{ tasks.length }} 条发现记录</span>
        <button
          type="button"
          data-test="refresh-tasks"
          :disabled="tasksLoading"
          @click="loadTasks"
        >{{ tasksLoading ? "载入中…" : "刷新" }}</button>
      </div>

      <div v-if="tasksLoading && tasks.length === 0" class="empty-state">正在读取发现记录…</div>
      <div v-else-if="!error && tasks.length === 0" class="empty-state">暂无发现记录。</div>

      <div class="task-grid" v-else>
        <article
          v-for="task in tasks"
          :key="task.id"
          class="task-card"
          :data-test="`task-${task.id}`"
        >
          <div class="task-head">
            <span class="status-badge" :class="`status-${task.status}`" data-test="task-status">
              {{ computeTaskStatusLabel(task.status) }}
            </span>
            <span class="source-label">{{ task.source_name || task.source_key }}</span>
          </div>

          <a
            v-if="task.source_url"
            :href="task.source_url"
            target="_blank"
            rel="noopener noreferrer"
            class="task-url"
            :title="task.source_url"
          >{{ task.source_url.replace(/^https?:\/\//, '').substring(0, 60) }}…</a>

          <div class="task-meta">
            <span>尝试 {{ task.attempt_count }} 次</span>
            <span v-if="task.block_reason" class="block-reason" data-test="block-reason">
              阻塞: {{ task.block_reason }}
            </span>
          </div>

          <div v-if="task.result_summary_json" class="task-summary" data-test="task-summary">
            <pre>{{ JSON.stringify(task.result_summary_json, null, 2) }}</pre>
          </div>

          <button
            v-if="!isRunning(task.status)"
            type="button"
            class="retry-btn"
            data-test="retry-task"
            :disabled="actionBusy[task.id]"
            @click="doRetry(task.id)"
          >{{ actionBusy[task.id] ? "处理中…" : "重试" }}</button>
        </article>
      </div>
    </div>

    <!-- Groups Tab -->
    <div v-if="tab === 'groups'">
      <div class="toolbar">
        <span>共 {{ groups.length }} 个相似分组</span>
        <button
          type="button"
          data-test="refresh-groups"
          :disabled="groupsLoading"
          @click="loadGroups"
        >{{ groupsLoading ? "载入中…" : "刷新" }}</button>
      </div>

      <div v-if="groupsLoading && groups.length === 0" class="empty-state">正在读取审核分组…</div>
      <div v-else-if="!error && groups.length === 0" class="empty-state">暂无待审核分组。</div>

      <div class="groups-list" v-else>
        <section
          v-for="group in groups"
          :key="group.similarity_group_key"
          class="group-card"
          :data-test="`group-${group.similarity_group_key}`"
        >
          <h3 class="group-heading">
            <span class="group-key">分组: {{ group.similarity_group_key }}</span>
            <span class="candidate-count">{{ group.candidates.length }} 个候选</span>
          </h3>

          <div
            v-for="candidate in group.candidates"
            :key="candidate.id"
            class="candidate-card"
            :data-test="`candidate-${candidate.id}`"
          >
            <div class="candidate-head">
              <span class="status-badge" :class="`status-${candidate.status}`">
                {{ computeCandidateStatusLabel(candidate.status) }}
              </span>
              <strong>{{ candidate.company_name || "—" }}</strong>
              <span class="candidate-title">{{ candidate.title || "—" }}</span>
              <span
                v-if="candidate.confidence !== null && candidate.confidence !== undefined"
                class="confidence"
                :class="confidenceClass(candidate.confidence)"
              >{{ Math.round(candidate.confidence * 100) }}%</span>
            </div>

            <div v-if="candidate.locations_json && candidate.locations_json.length" class="candidate-detail">
              <span class="detail-label">地点:</span>
              <span>{{ candidate.locations_json.join("、") }}</span>
            </div>

            <div v-if="candidate.apply_url" class="candidate-detail">
              <span class="detail-label">投递入口:</span>
              <a :href="candidate.apply_url" target="_blank" rel="noopener noreferrer">{{ candidate.apply_url }}</a>
            </div>

            <div v-if="candidate.description_text" class="candidate-detail">
              <span class="detail-label">描述:</span>
              <p class="description-excerpt">{{ candidate.description_text.substring(0, 200) }}{{ candidate.description_text.length > 200 ? "…" : "" }}</p>
            </div>

            <!-- Evidence -->
            <div
              v-if="candidate.evidence_refs_json && candidate.evidence_refs_json.length"
              class="evidence-section"
              data-test="evidence-section"
            >
              <span class="detail-label">证据:</span>
              <ul>
                <li v-for="(ev, idx) in candidate.evidence_refs_json" :key="idx">
                  <a
                    v-if="ev.url"
                    :href="ev.url"
                    target="_blank"
                    rel="noopener noreferrer"
                  >{{ ev.title || ev.url }}</a>
                  <span v-else>{{ ev.title || ev.type || "证据" }}</span>
                  <p v-if="ev.excerpt" class="evidence-excerpt">{{ ev.excerpt.substring(0, 150) }}{{ ev.excerpt.length > 150 ? "…" : "" }}</p>
                </li>
              </ul>
            </div>

            <!-- Normalization warnings -->
            <div
              v-if="candidate.normalization_warnings_json && candidate.normalization_warnings_json.length"
              class="warnings-section"
              data-test="warnings-section"
            >
              <span class="detail-label warning-label">规约警告:</span>
              <ul>
                <li v-for="(w, idx) in candidate.normalization_warnings_json" :key="idx">{{ w }}</li>
              </ul>
            </div>

            <!-- Actions -->
            <div v-if="candidate.status === 'pending_review'" class="candidate-actions">
              <button
                type="button"
                class="approve-btn"
                data-test="approve-candidate"
                :disabled="actionBusy[`approve:${candidate.id}`] || actionBusy[`reject:${candidate.id}`]"
                @click="doApprove(candidate)"
              >{{ actionBusy[`approve:${candidate.id}`] ? "处理中…" : "通过" }}</button>
              <button
                type="button"
                class="reject-btn"
                data-test="reject-candidate"
                :disabled="actionBusy[`approve:${candidate.id}`] || actionBusy[`reject:${candidate.id}`]"
                @click="doReject(candidate)"
              >{{ actionBusy[`reject:${candidate.id}`] ? "处理中…" : "拒绝" }}</button>
            </div>
          </div>
        </section>
      </div>
    </div>
  </section>
</template>

<script lang="ts">
export function confidenceClass(confidence: number): string {
  if (confidence >= 0.7) return "confidence-high";
  if (confidence >= 0.4) return "confidence-mid";
  return "confidence-low";
}
</script>

<style scoped>
.discovery-review {
  --ink: #17372d;
  --muted: #6e7c75;
  --paper: #fffdf8;
  --line: #d8ddd6;
  --green: #17634e;
  --amber: #d98b24;
  --red: #b13e3e;
  color: var(--ink);
  padding: clamp(1rem, 2vw, 1.8rem);
  border: 1px solid var(--line);
  border-radius: 24px;
  background:
    linear-gradient(135deg, rgba(23, 99, 78, 0.05), transparent 42%),
    var(--paper);
  box-shadow: 0 22px 55px rgba(23, 55, 45, 0.1);
}

.review-header,
.toolbar,
.mode-switch {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.review-header {
  justify-content: space-between;
  margin-bottom: 1rem;
}

.review-header h1 {
  margin: 0.2rem 0;
  font-family: Georgia, "Noto Serif SC", serif;
}

.review-header p { margin: 0; color: var(--muted); }
.kicker { color: var(--green) !important; font-size: 0.75rem; font-weight: 900; letter-spacing: 0.14em; }

button,
select,
input,
textarea {
  font: inherit;
}

button {
  border: 1px solid var(--line);
  border-radius: 10px;
  padding: 0.65rem 0.85rem;
  color: var(--ink);
  background: white;
  cursor: pointer;
}

button:disabled { cursor: not-allowed; opacity: 0.48; }

.mode-switch { padding: 0.25rem; border-radius: 14px; background: #edf2ed; }
.mode-switch button { border: 0; background: transparent; }
.mode-switch .active { color: white; background: var(--green); }

.toolbar {
  justify-content: flex-end;
  padding: 0.75rem;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  gap: 0.75rem;
}
.toolbar span { margin-right: auto; }

.notice { padding: 0.75rem 0.9rem; border-radius: 10px; margin: 0.5rem 0; }
.notice.error { color: #8c2828; background: #ffebeb; }
.notice.success { color: #185c43; background: #e5f5ed; }

.empty-state { padding: 2rem 1rem; color: var(--muted); text-align: center; }

/* Task grid */
.task-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 1rem;
  margin-top: 1rem;
}

.task-card {
  border: 1px solid var(--line);
  border-radius: 18px;
  padding: 1rem;
  background: rgba(255, 255, 255, 0.8);
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.task-head {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.status-badge {
  width: fit-content;
  padding: 0.18rem 0.45rem;
  border-radius: 999px;
  font: 700 0.72rem/1.4 ui-monospace, monospace;
  background: #e2f1e9;
  color: var(--green);
}
.status-badge.status-running { background: #e0edff; color: #1a5bbf; }
.status-badge.status-failed { background: #ffebeb; color: var(--red); }
.status-badge.status-needs_manual_review { background: #fff0d6; color: #8a4d00; }
.status-badge.status-partial_success { background: #fff0d6; color: #8a4d00; }
.status-badge.status-cancelled { background: #eee; color: #666; }
.status-badge.status-queued { background: #edf2f5; color: #4a5b6a; }

.source-label { font-weight: 700; color: var(--ink); }

.task-url {
  color: var(--green);
  font-size: 0.85rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.task-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  font-size: 0.82rem;
  color: var(--muted);
}

.block-reason { color: var(--amber); }

.task-summary {
  margin: 0.25rem 0;
  padding: 0.5rem;
  background: #f4f7f2;
  border-radius: 8px;
  max-height: 120px;
  overflow: auto;
}
.task-summary pre {
  margin: 0;
  font-size: 0.75rem;
  white-space: pre-wrap;
}

.retry-btn {
  align-self: flex-end;
  margin-top: auto;
}

/* Groups */
.groups-list {
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
  margin-top: 1rem;
}

.group-card {
  border: 1px solid var(--line);
  border-radius: 18px;
  padding: 1rem;
  background: rgba(255, 255, 255, 0.8);
}

.group-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 0 0 0.75rem;
  font-family: Georgia, "Noto Serif SC", serif;
}
.group-key { font-size: 1rem; }
.candidate-count { font-size: 0.82rem; color: var(--muted); font-weight: 400; }

.candidate-card {
  border: 1px solid var(--line);
  border-radius: 14px;
  padding: 0.85rem;
  margin-bottom: 0.65rem;
  background: #fbfcf9;
}

.candidate-head {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex-wrap: wrap;
  margin-bottom: 0.35rem;
}

.candidate-title { color: var(--muted); }

.confidence {
  margin-left: auto;
  font-weight: 700;
  font-size: 0.82rem;
}
.confidence-high { color: var(--green); }
.confidence-mid { color: var(--amber); }
.confidence-low { color: var(--red); }

.candidate-detail {
  display: flex;
  gap: 0.35rem;
  font-size: 0.85rem;
  margin-top: 0.25rem;
  align-items: flex-start;
}

.detail-label { color: var(--muted); font-weight: 600; min-width: 4em; white-space: nowrap; }
.warning-label { color: var(--amber); }

.description-excerpt {
  margin: 0;
  color: #444;
  line-height: 1.4;
}

.evidence-section {
  margin-top: 0.5rem;
  padding: 0.5rem;
  background: #edf5ef;
  border-radius: 8px;
}

.evidence-section ul,
.warnings-section ul {
  margin: 0.25rem 0 0;
  padding-left: 1.25rem;
  font-size: 0.82rem;
}

.evidence-section li,
.warnings-section li {
  margin-bottom: 0.25rem;
}

.evidence-excerpt {
  margin: 0.15rem 0 0;
  font-style: italic;
  color: #555;
  font-size: 0.8rem;
}

.warnings-section {
  margin-top: 0.25rem;
  color: var(--amber);
}

.candidate-actions {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.65rem;
  justify-content: flex-end;
}

.approve-btn { color: white; background: var(--green); border-color: var(--green); }
.reject-btn { color: var(--red); border-color: var(--red); }

@media (max-width: 600px) {
  .task-grid { grid-template-columns: 1fr; }
  .review-header,
  .toolbar { flex-direction: column; align-items: stretch; }
  .toolbar span { margin-right: 0; }
  .candidate-head { flex-direction: column; align-items: flex-start; }
  .confidence { margin-left: 0; }
}
</style>
