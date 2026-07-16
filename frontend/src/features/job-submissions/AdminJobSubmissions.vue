<script setup lang="ts">
import { onMounted, ref } from "vue";

import {
  decideJobSubmission,
  fetchAdminJobSubmissions,
  fetchDuplicateCandidates,
} from "./jobSubmissionsApi";
import type { AdminJobSubmission, DuplicateCandidate } from "./jobSubmissionTypes";

const props = defineProps<{ token: string }>();

const emit = defineEmits<{ dirtyChange: [value: boolean] }>();

const submissions = ref<AdminJobSubmission[]>([]);
const total = ref(0);
const page = ref(1);
const loading = ref(false);
const error = ref("");
const success = ref("");

const selectedId = ref("");
const candidates = ref<DuplicateCandidate[]>([]);
const candidatesLoading = ref(false);

type DecisionAction = "link_existing" | "create_pending" | "reject";
const decisionAction = ref<DecisionAction>("link_existing");
const decisionVersion = ref(0);
const linkJobId = ref("");
const newCompanyName = ref("");
const newTitle = ref("");
const newApplyUrl = ref("");
const rejectReason = ref<"not_a_job" | "insufficient_evidence" | "unsafe_link" | "duplicate_submission">("not_a_job");
const deciding = ref(false);
const decisionError = ref("");

const PAGE_SIZE = 20;

async function loadQueue() {
  loading.value = true;
  error.value = "";
  try {
    const response = await fetchAdminJobSubmissions(props.token, PAGE_SIZE, (page.value - 1) * PAGE_SIZE);
    submissions.value = response.submissions;
    total.value = response.total;
  } catch (caught) {
    submissions.value = [];
    total.value = 0;
    error.value = caught instanceof Error ? caught.message : "审核队列加载失败。";
  } finally {
    loading.value = false;
  }
}

async function loadCandidates(submissionId: string) {
  if (selectedId.value === submissionId) {
    selectedId.value = "";
    return;
  }
  selectedId.value = submissionId;
  candidates.value = [];
  candidatesLoading.value = true;
  try {
    const response = await fetchDuplicateCandidates(props.token, submissionId);
    candidates.value = response.candidates;
    const item = submissions.value.find((s) => s.id === submissionId);
    if (item) {
      decisionVersion.value = item.version;
    }
    resetDecision();
  } catch {
    candidates.value = [];
  } finally {
    candidatesLoading.value = false;
  }
}

function resetDecision() {
  decisionAction.value = "link_existing";
  linkJobId.value = "";
  newCompanyName.value = "";
  newTitle.value = "";
  newApplyUrl.value = "";
  rejectReason.value = "not_a_job";
  decisionError.value = "";
}

async function handleDecision() {
  if (!selectedId.value) return;
  deciding.value = true;
  decisionError.value = "";
  try {
    let payload: any;
    if (decisionAction.value === "link_existing") {
      if (!linkJobId.value.trim()) {
        decisionError.value = "请输入职位 ID。";
        deciding.value = false;
        return;
      }
      payload = { expected_version: decisionVersion.value, action: "link_existing", job_id: linkJobId.value.trim() };
    } else if (decisionAction.value === "create_pending") {
      if (!newCompanyName.value.trim() || !newTitle.value.trim()) {
        decisionError.value = "公司名称和职位名称不能为空。";
        deciding.value = false;
        return;
      }
      const body: Record<string, string> = { expected_version: String(decisionVersion.value), action: "create_pending", company_name: newCompanyName.value.trim(), title: newTitle.value.trim() };
      if (newApplyUrl.value.trim()) body.apply_url = newApplyUrl.value.trim();
      payload = body;
    } else {
      payload = { expected_version: decisionVersion.value, action: "reject", reason_code: rejectReason.value };
    }
    await decideJobSubmission(props.token, selectedId.value, payload as any);
    success.value = "处理完成。";
    emit("dirtyChange", true);
    selectedId.value = "";
    await loadQueue();
  } catch (caught) {
    decisionError.value = caught instanceof Error ? caught.message : "处理失败。";
  } finally {
    deciding.value = false;
  }
}

function statusLabel(status: string): string {
  switch (status) {
    case "draft": return "草稿";
    case "submitted": return "待审核";
    case "promoted": return "已采纳";
    case "rejected": return "已拒绝";
    default: return status;
  }
}

function formatDate(value: string | null): string {
  return value ? value.slice(0, 10) : "";
}

const totalPages = () => Math.max(1, Math.ceil(total.value / PAGE_SIZE));

function changePage(next: number) {
  page.value = next;
  loadQueue();
}

onMounted(loadQueue);
</script>

<template>
  <section class="admin-queue" aria-labelledby="admin-queue-title">
    <header class="section-header">
      <div>
        <p class="eyebrow">ADMIN SUBMISSION QUEUE</p>
        <h2 id="admin-queue-title">用户提交审核</h2>
        <p>审阅用户提交的职位信息，链接到已有职位或创建新职位。</p>
      </div>
    </header>

    <p v-if="success" class="feedback success">{{ success }}</p>

    <p v-if="loading" class="state-card" role="status">正在加载审核队列…</p>
    <div v-else-if="error" class="state-card error" role="alert">
      <strong>{{ error }}</strong>
      <button type="button" @click="loadQueue">重新加载</button>
    </div>
    <div v-else-if="submissions.length === 0" class="state-card empty">
      <strong>审核队列为空。</strong>
      <span>暂无待审核的用户提交。</span>
    </div>

    <div v-else class="queue-list">
      <article
        v-for="item in submissions" :key="item.id"
        class="queue-card"
        :class="{ selected: selectedId === item.id }"
      >
        <div class="card-main">
          <div class="card-info">
            <span class="input-type-label">
              {{ item.input_type === "url" ? "链接" : "JD" }}
            </span>
            <p class="preview">{{ item.input_preview }}</p>
            <p class="meta">
              版本 {{ item.version }} · {{ formatDate(item.created_at) }}
            </p>
          </div>
          <div class="card-actions">
            <button
              class="action-btn ghost"
              type="button"
              @click="loadCandidates(item.id)"
            >
              {{ selectedId === item.id ? "收起" : "审核" }}
            </button>
          </div>
        </div>

        <div v-if="selectedId === item.id" class="decision-section">
          <div v-if="candidatesLoading" class="state-card" role="status">正在加载候选…</div>
          <div v-else-if="candidates.length > 0" class="candidates-list">
            <p class="section-label">重复检测候选</p>
            <div
              v-for="c in candidates" :key="c.job.id"
              class="candidate-row"
            >
              <div class="candidate-info">
                <strong>{{ c.job.company_name }}</strong> · {{ c.job.title }}
                <span class="status-tag" :class="c.job.status">{{ c.job.status }}</span>
                <span class="score">{{ (c.score_basis_points / 100).toFixed(0) }}%</span>
              </div>
              <code class="job-id">{{ c.job.id }}</code>
            </div>
          </div>

          <div class="decision-form">
            <p class="section-label">管理员决策</p>

            <div class="decision-tabs">
              <button
                type="button" :class="{ active: decisionAction === 'link_existing' }"
                @click="decisionAction = 'link_existing'"
              >链接已有职位</button>
              <button
                type="button" :class="{ active: decisionAction === 'create_pending' }"
                @click="decisionAction = 'create_pending'"
              >创建待补全</button>
              <button
                type="button" :class="{ active: decisionAction === 'reject' }"
                @click="decisionAction = 'reject'"
              >拒绝</button>
            </div>

            <div v-if="decisionAction === 'link_existing'" class="decision-fields">
              <label>
                职位 ID
                <input v-model="linkJobId" placeholder="00000000-0000-4000-8000-000000000001" />
              </label>
            </div>

            <div v-if="decisionAction === 'create_pending'" class="decision-fields">
              <label>
                公司名称
                <input v-model="newCompanyName" placeholder="示例科技" />
              </label>
              <label>
                职位名称
                <input v-model="newTitle" placeholder="后端实习生" />
              </label>
              <label>
                投递链接（可选）
                <input v-model="newApplyUrl" placeholder="https://jobs.example.com/apply" />
              </label>
            </div>

            <div v-if="decisionAction === 'reject'" class="decision-fields">
              <label>
                拒绝原因
                <select v-model="rejectReason">
                  <option value="not_a_job">非职位信息</option>
                  <option value="insufficient_evidence">证据不足</option>
                  <option value="unsafe_link">不安全链接</option>
                  <option value="duplicate_submission">重复提交</option>
                </select>
              </label>
            </div>

            <p v-if="decisionError" class="feedback error">{{ decisionError }}</p>

            <div class="form-actions">
              <button
                class="primary-action" type="button"
                :disabled="deciding"
                @click="handleDecision"
              >{{ deciding ? "处理中…" : "确认" }}</button>
            </div>
          </div>
        </div>
      </article>
    </div>

    <nav v-if="total > PAGE_SIZE" class="pagination" aria-label="分页">
      <button
        class="secondary-action" type="button"
        :disabled="page <= 1"
        @click="changePage(page - 1)"
      >上一页</button>
      <span>第 {{ page }} / {{ totalPages() }} 页</span>
      <button
        class="secondary-action" type="button"
        :disabled="page * PAGE_SIZE >= total"
        @click="changePage(page + 1)"
      >下一页</button>
    </nav>
  </section>
</template>

<style scoped>
.admin-queue {
  --ink: #1d2925;
  --muted: #697873;
  --line: #dce5df;
  --pine: #1c6650;
  --pine-dark: #104938;
  --paper: #fffef9;
  display: flex;
  flex-direction: column;
  gap: 1.25rem;
  padding: clamp(1.25rem, 3vw, 2rem);
  color: var(--ink);
  background: linear-gradient(125deg, rgba(226, 238, 229, 0.72), transparent 40%), var(--paper);
  border: 1px solid var(--line);
  border-radius: 26px;
  box-shadow: 0 20px 50px rgba(28, 71, 56, 0.09);
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.section-header h2 {
  margin: 0;
  font-family: Georgia, "Songti SC", serif;
  font-size: clamp(2rem, 4vw, 3rem);
  line-height: 1.08;
}

.section-header > div > p:last-child {
  margin-top: 0.6rem;
  color: var(--muted);
}

.eyebrow {
  color: var(--pine);
  font-size: 0.76rem;
  font-weight: 800;
  letter-spacing: 0.16em;
}

.feedback {
  margin: 0;
  padding: 0.7rem 1rem;
  border-radius: 11px;
  font-size: 0.86rem;
}

.feedback.success {
  background: #dcfce7;
  color: #166534;
}

.feedback.error {
  background: #fee2e2;
  color: #991b1b;
}

.state-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.5rem;
  min-height: 6rem;
  padding: 1.4rem;
  justify-content: center;
  color: var(--muted);
  background: rgba(255, 255, 255, 0.75);
  border: 1px dashed #cbd8cf;
  border-radius: 18px;
}

.state-card.error {
  color: #8c2929;
  background: #fff8f5;
  border-color: #eac4bd;
}

.state-card.empty {
  color: var(--muted);
}

.queue-list {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.queue-card {
  display: flex;
  flex-direction: column;
  padding: 1rem;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid var(--line);
  border-radius: 18px;
}

.queue-card:hover {
  border-color: #b9d0c1;
}

.queue-card.selected {
  border-color: var(--pine);
  box-shadow: 0 0 0 2px rgba(28, 102, 80, 0.12);
}

.card-main {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 1rem;
}

.card-info {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.45rem;
  min-width: 0;
}

.input-type-label {
  display: inline-flex;
  padding: 0.2rem 0.5rem;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 700;
  line-height: 1.3;
  background: #f1f4f1;
  color: #56645f;
}

.preview {
  width: 100%;
  margin: 0.2rem 0 0;
  font-size: 0.9rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.meta {
  width: 100%;
  margin: 0.15rem 0 0;
  font-size: 0.78rem;
  color: var(--muted);
}

.card-actions {
  flex-shrink: 0;
}

.action-btn {
  padding: 0.5rem 0.85rem;
  border-radius: 11px;
  font-size: 0.86rem;
  font-weight: 750;
  cursor: pointer;
  transition: transform 140ms ease, box-shadow 140ms ease;
}

.action-btn.ghost {
  color: var(--pine);
  background: #f8fbf8;
  border: 1px solid #cddbd2;
}

.action-btn:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: 0 8px 18px rgba(28, 71, 56, 0.13);
}

.decision-section {
  display: flex;
  flex-direction: column;
  gap: 1rem;
  margin-top: 0.8rem;
  padding-top: 0.8rem;
  border-top: 1px solid var(--line);
}

.section-label {
  margin: 0;
  font-size: 0.86rem;
  font-weight: 700;
  color: var(--muted);
}

.candidates-list {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}

.candidate-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
  padding: 0.4rem 0;
  border-bottom: 1px solid #f0f4f0;
  font-size: 0.88rem;
}

.candidate-row:last-child {
  border-bottom: none;
}

.candidate-info {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem;
  min-width: 0;
}

.status-tag {
  display: inline-flex;
  padding: 0.1rem 0.4rem;
  border-radius: 999px;
  font-size: 0.72rem;
  font-weight: 700;
}

.status-tag.verified {
  background: #dcfce7;
  color: #166534;
}

.status-tag.pending_completion {
  background: #fef3c7;
  color: #92400e;
}

.score {
  display: inline-flex;
  padding: 0.1rem 0.4rem;
  border-radius: 999px;
  background: #e6f4eb;
  color: #1a624d;
  font-size: 0.78rem;
  font-weight: 700;
}

.job-id {
  font-size: 0.72rem;
  color: var(--muted);
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.decision-form {
  display: flex;
  flex-direction: column;
  gap: 0.8rem;
  padding: 1rem;
  background: #f4f8f4;
  border: 1px solid #cadace;
  border-radius: 14px;
}

.decision-tabs {
  display: inline-flex;
  gap: 0.35rem;
  padding: 0.25rem;
  background: #e8f0ea;
  border-radius: 12px;
}

.decision-tabs button {
  padding: 0.45rem 0.85rem;
  border: none;
  border-radius: 10px;
  background: transparent;
  color: var(--muted);
  font-weight: 700;
  font-size: 0.83rem;
  cursor: pointer;
}

.decision-tabs button.active {
  background: white;
  color: var(--pine-dark);
  box-shadow: 0 2px 8px rgba(28, 71, 56, 0.1);
}

.decision-fields {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.decision-fields label {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  font-size: 0.83rem;
  font-weight: 700;
  color: var(--muted);
}

.decision-fields input,
.decision-fields select {
  padding: 0.6rem 0.75rem;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: white;
  font: inherit;
  color: var(--ink);
  outline: none;
}

.decision-fields input:focus,
.decision-fields select:focus {
  border-color: var(--pine);
  box-shadow: 0 0 0 3px rgba(28, 102, 80, 0.12);
}

.form-actions {
  display: flex;
  gap: 0.75rem;
}

.primary-action, .secondary-action {
  padding: 0.68rem 0.85rem;
  border-radius: 11px;
  font-size: 0.86rem;
  font-weight: 750;
  cursor: pointer;
  transition: transform 140ms ease, box-shadow 140ms ease;
}

.primary-action {
  color: white;
  background: var(--pine);
  border: 1px solid var(--pine);
}

.secondary-action {
  color: var(--pine-dark);
  background: #f8fbf8;
  border: 1px solid #cddbd2;
}

.primary-action:hover:not(:disabled),
.secondary-action:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: 0 8px 18px rgba(28, 71, 56, 0.13);
}

.primary-action:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.pagination {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 1rem;
  color: var(--muted);
  font-size: 0.86rem;
}

@media (max-width: 600px) {
  .card-main {
    flex-direction: column;
  }
}
</style>
