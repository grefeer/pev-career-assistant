<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";

import {
  createJobSubmission,
  fetchDuplicateCandidates,
  fetchJobSubmissions,
  submitJobSubmission,
  updateJobSubmission,
} from "./jobSubmissionsApi";
import type { DuplicateCandidate, JobSubmission } from "./jobSubmissionTypes";

const props = defineProps<{ token: string }>();

const submissions = ref<JobSubmission[]>([]);
const total = ref(0);
const page = ref(1);
const loading = ref(false);
const error = ref("");
const success = ref("");

const newInputType = ref<"url" | "jd_text">("url");
const newUrl = ref("");
const newJdText = ref("");
const creating = ref(false);
const createError = ref("");

const selectedId = ref("");
const candidates = ref<DuplicateCandidate[]>([]);
const candidatesLoading = ref(false);
const candidatesError = ref("");

const editingId = ref("");
const editInputType = ref<"url" | "jd_text">("url");
const editUrl = ref("");
const editJdText = ref("");
const editVersion = ref(0);
const saving = ref(false);

let isMounted = true;
const PAGE_SIZE = 10;

async function loadSubmissions() {
  loading.value = true;
  error.value = "";
  try {
    const response = await fetchJobSubmissions(props.token, PAGE_SIZE, (page.value - 1) * PAGE_SIZE);
    if (!isMounted) return;
    submissions.value = response.submissions;
    total.value = response.total;
  } catch (caught) {
    if (!isMounted) return;
    submissions.value = [];
    total.value = 0;
    error.value = caught instanceof Error ? caught.message : "提交列表加载失败。";
  } finally {
    if (isMounted) loading.value = false;
  }
}

async function handleCreate() {
  if (newInputType.value === "url" && !newUrl.value.trim()) return;
  if (newInputType.value === "jd_text" && !newJdText.value.trim()) return;
  creating.value = true;
  createError.value = "";
  try {
    const payload = newInputType.value === "url"
      ? { input_type: "url" as const, url: newUrl.value.trim() }
      : { input_type: "jd_text" as const, jd_text: newJdText.value.trim() };
    await createJobSubmission(props.token, payload);
    if (!isMounted) return;
    newUrl.value = "";
    newJdText.value = "";
    success.value = "提交已创建。";
    selectedId.value = "";
    page.value = 1;
    await loadSubmissions();
  } catch (caught) {
    if (!isMounted) return;
    createError.value = caught instanceof Error ? caught.message : "创建提交失败。";
  } finally {
    if (isMounted) creating.value = false;
  }
}

function startEdit(item: JobSubmission) {
  editingId.value = item.id;
  editInputType.value = item.input_type;
  editUrl.value = item.normalized_url || "";
  editJdText.value = item.input_preview;
  editVersion.value = item.version;
}

function cancelEdit() {
  editingId.value = "";
}

async function handleUpdate() {
  if (!editingId.value) return;
  saving.value = true;
  createError.value = "";
  try {
    const payload = editInputType.value === "url"
      ? { input_type: "url" as const, url: editUrl.value.trim() }
      : { input_type: "jd_text" as const, jd_text: editJdText.value.trim() };
    await updateJobSubmission(props.token, editingId.value, editVersion.value, payload);
    if (!isMounted) return;
    editingId.value = "";
    success.value = "已更新草稿。";
    selectedId.value = "";
    await loadSubmissions();
  } catch (caught) {
    if (!isMounted) return;
    createError.value = caught instanceof Error ? caught.message : "更新草稿失败。";
  } finally {
    if (isMounted) saving.value = false;
  }
}

async function handleSubmit(item: JobSubmission) {
  loading.value = true;
  try {
    await submitJobSubmission(props.token, item.id, item.version);
    if (!isMounted) return;
    success.value = "已提交审核。";
    await loadSubmissions();
  } catch (caught) {
    if (!isMounted) return;
    error.value = caught instanceof Error ? caught.message : "提交审核失败。";
  } finally {
    if (isMounted) loading.value = false;
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
  candidatesError.value = "";
  try {
    const response = await fetchDuplicateCandidates(props.token, submissionId);
    if (!isMounted) return;
    candidates.value = response.candidates;
  } catch (caught) {
    if (!isMounted) return;
    candidatesError.value = caught instanceof Error ? caught.message : "候选列表加载失败。";
  } finally {
    if (isMounted) candidatesLoading.value = false;
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

function dedupLabel(status: string | null): string {
  switch (status) {
    case "succeeded": return "查重完成";
    case "failed": return "查重失败";
    case "pending": return "查重中";
    default: return status || "";
  }
}

function formatDate(value: string | null): string {
  return value ? value.slice(0, 10) : "";
}

const totalPages = () => Math.max(1, Math.ceil(total.value / PAGE_SIZE));
const hasPreviousPage = () => page.value > 1;
const hasNextPage = () => page.value * PAGE_SIZE < total.value;

function changePage(next: number) {
  page.value = next;
  loadSubmissions();
}

onMounted(loadSubmissions);
onUnmounted(() => { isMounted = false; });
</script>

<template>
  <section class="job-submissions" aria-labelledby="submissions-title">
    <header class="section-header">
      <div>
        <p class="eyebrow">JOB SUBMISSIONS</p>
        <h2 id="submissions-title">我的职位提交</h2>
        <p>提交职位链接或 JD 文本，系统将自动检测重复并交由管理员核验。</p>
      </div>
    </header>

    <form class="create-form" @submit.prevent="handleCreate">
      <div class="input-type-toggle">
        <button
          type="button" :class="{ active: newInputType === 'url' }"
          @click="newInputType = 'url'"
        >职位链接</button>
        <button
          type="button" :class="{ active: newInputType === 'jd_text' }"
          @click="newInputType = 'jd_text'"
        >JD 文本</button>
      </div>
      <textarea
        v-if="newInputType === 'jd_text'"
        v-model="newJdText" rows="4"
        placeholder="粘贴职位描述文本…"
        class="input-field"
      />
      <input
        v-else v-model="newUrl"
        placeholder="https://jobs.example.com/opening"
        class="input-field"
      />
      <div class="form-actions">
        <button
          class="primary-action" type="submit" :disabled="creating"
        >{{ creating ? "提交中…" : "创建提交" }}</button>
      </div>
      <p v-if="createError" class="feedback error">{{ createError }}</p>
    </form>

    <p v-if="success" class="feedback success">{{ success }}</p>

    <div class="list-section">
      <div class="list-meta">
        <span>共 {{ total }} 条提交</span>
      </div>

      <p v-if="loading" class="state-card" role="status">正在加载…</p>
      <div v-else-if="error" class="state-card error" role="alert">
        <strong>{{ error }}</strong>
        <button type="button" @click="loadSubmissions">重新加载</button>
      </div>
      <div v-else-if="submissions.length === 0" class="state-card empty">
        <strong>暂无提交记录。</strong>
        <span>使用上方表单提交职位链接或 JD 文本。</span>
      </div>

      <div v-else class="submission-list">
        <article
          v-for="item in submissions" :key="item.id"
          class="submission-card"
          :class="{ selected: selectedId === item.id }"
        >
          <div class="card-main">
            <div class="card-info">
              <span class="status-badge" :class="item.status">
                {{ statusLabel(item.status) }}
              </span>
              <span class="dedup-badge" :class="item.deduplication_status">
                {{ dedupLabel(item.deduplication_status) }}
              </span>
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
                v-if="item.status === 'draft'"
                class="action-btn secondary"
                type="button"
                @click="startEdit(item)"
              >编辑</button>
              <button
                v-if="item.status === 'draft'"
                class="action-btn primary"
                type="button"
                :disabled="loading"
                @click="handleSubmit(item)"
              >提交审核</button>
              <button
                class="action-btn ghost"
                type="button"
                @click="loadCandidates(item.id)"
              >
                {{ selectedId === item.id ? "收起" : "候选" }}
              </button>
            </div>
          </div>

          <div v-if="editingId === item.id" class="edit-section">
            <div class="input-type-toggle">
              <button
                type="button" :class="{ active: editInputType === 'url' }"
                @click="editInputType = 'url'"
              >职位链接</button>
              <button
                type="button" :class="{ active: editInputType === 'jd_text' }"
                @click="editInputType = 'jd_text'"
              >JD 文本</button>
            </div>
            <textarea
              v-if="editInputType === 'jd_text'"
              v-model="editJdText" rows="4" class="input-field"
            />
            <input
              v-else v-model="editUrl" class="input-field"
            />
            <div class="form-actions">
              <button
                class="primary-action" type="button" :disabled="saving"
                @click="handleUpdate"
              >{{ saving ? "保存中…" : "保存" }}</button>
              <button
                class="secondary-action" type="button"
                @click="cancelEdit"
              >取消</button>
            </div>
          </div>

          <div v-if="selectedId === item.id && editingId !== item.id" class="candidates-section">
            <p v-if="candidatesLoading" class="state-card" role="status">正在加载候选列表…</p>
            <p v-else-if="candidatesError" class="state-card error">{{ candidatesError }}</p>
            <div v-else-if="candidates.length === 0" class="state-card empty">
              未发现重复候选。
            </div>
            <div v-else>
              <p class="candidates-heading">重复检测候选（仅显示已核验职位）</p>
              <div
                v-for="c in candidates" :key="c.job.id"
                class="candidate-row"
              >
                <div class="candidate-info">
                  <strong>{{ c.job.company_name }}</strong> · {{ c.job.title }}
                  <span class="score">{{ (c.score_basis_points / 100).toFixed(0) }}%</span>
                </div>
                <a :href="c.job.apply_url" target="_blank" rel="noopener noreferrer">
                  查看
                </a>
              </div>
            </div>
          </div>
        </article>
      </div>

      <nav v-if="total > PAGE_SIZE" class="pagination" aria-label="分页">
        <button
          class="secondary-action" type="button"
          :disabled="!hasPreviousPage()"
          @click="changePage(page - 1)"
        >上一页</button>
        <span>第 {{ page }} / {{ totalPages() }} 页</span>
        <button
          class="secondary-action" type="button"
          :disabled="!hasNextPage()"
          @click="changePage(page + 1)"
        >下一页</button>
      </nav>
    </div>
  </section>
</template>

<style scoped>
.job-submissions {
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

.create-form {
  display: flex;
  flex-direction: column;
  gap: 0.8rem;
  padding: 1rem;
  background: rgba(255, 255, 255, 0.72);
  border: 1px solid var(--line);
  border-radius: 18px;
}

.input-type-toggle {
  display: inline-flex;
  gap: 0.35rem;
  padding: 0.25rem;
  background: #f1f4f1;
  border-radius: 12px;
}

.input-type-toggle button {
  padding: 0.5rem 1rem;
  border: none;
  border-radius: 10px;
  background: transparent;
  color: var(--muted);
  font-weight: 700;
  font-size: 0.85rem;
  cursor: pointer;
}

.input-type-toggle button.active {
  background: white;
  color: var(--pine-dark);
  box-shadow: 0 2px 8px rgba(28, 71, 56, 0.1);
}

.input-field {
  width: 100%;
  min-width: 0;
  padding: 0.72rem 0.8rem;
  color: var(--ink);
  background: white;
  border: 1px solid var(--line);
  border-radius: 11px;
  outline: none;
  font: inherit;
  box-sizing: border-box;
}

.input-field:focus {
  border-color: var(--pine);
  box-shadow: 0 0 0 3px rgba(28, 102, 80, 0.12);
}

textarea.input-field {
  resize: vertical;
}

.form-actions {
  display: flex;
  gap: 0.75rem;
}

.primary-action, .secondary-action, .action-btn {
  padding: 0.68rem 0.85rem;
  border-radius: 11px;
  font-size: 0.86rem;
  font-weight: 750;
  cursor: pointer;
  transition: transform 140ms ease, box-shadow 140ms ease;
}

.primary-action, .action-btn.primary {
  color: white;
  background: var(--pine);
  border: 1px solid var(--pine);
}

.secondary-action, .action-btn.secondary {
  color: var(--pine-dark);
  background: #f8fbf8;
  border: 1px solid #cddbd2;
}

.action-btn.ghost {
  color: var(--muted);
  background: transparent;
  border: 1px solid var(--line);
}

.primary-action:hover:not(:disabled),
.secondary-action:hover:not(:disabled),
.action-btn:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: 0 8px 18px rgba(28, 71, 56, 0.13);
}

.primary-action:disabled, .action-btn:disabled {
  cursor: not-allowed;
  opacity: 0.45;
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

.list-section {
  display: flex;
  flex-direction: column;
  gap: 0.8rem;
}

.list-meta {
  color: var(--muted);
  font-size: 0.86rem;
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

.submission-list {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.submission-card {
  display: flex;
  flex-direction: column;
  gap: 0;
  padding: 1rem;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid var(--line);
  border-radius: 18px;
  transition: border-color 140ms ease;
}

.submission-card:hover {
  border-color: #b9d0c1;
}

.submission-card.selected {
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

.status-badge, .dedup-badge, .input-type-label {
  display: inline-flex;
  padding: 0.2rem 0.5rem;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 700;
  line-height: 1.3;
}

.status-badge.draft {
  background: #f1f5f9;
  color: #475569;
}

.status-badge.submitted {
  background: #fef3c7;
  color: #92400e;
}

.status-badge.promoted {
  background: #dcfce7;
  color: #166534;
}

.status-badge.rejected {
  background: #fee2e2;
  color: #991b1b;
}

.dedup-badge.succeeded {
  background: #e6f4eb;
  color: #1a624d;
}

.dedup-badge.failed {
  background: #fff1d9;
  color: #7b5722;
}

.dedup-badge.pending {
  background: #f1f5f9;
  color: #64748b;
}

.input-type-label {
  background: #f1f4f1;
  color: #56645f;
}

.preview {
  width: 100%;
  margin: 0.35rem 0 0;
  font-size: 0.9rem;
  color: var(--ink);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.meta {
  width: 100%;
  margin: 0.25rem 0 0;
  font-size: 0.78rem;
  color: var(--muted);
}

.card-actions {
  display: flex;
  gap: 0.4rem;
  flex-shrink: 0;
}

.edit-section {
  display: flex;
  flex-direction: column;
  gap: 0.8rem;
  margin-top: 0.8rem;
  padding-top: 0.8rem;
  border-top: 1px solid var(--line);
}

.candidates-section {
  margin-top: 0.8rem;
  padding-top: 0.8rem;
  border-top: 1px solid var(--line);
}

.candidates-heading {
  margin: 0 0 0.5rem;
  font-size: 0.86rem;
  font-weight: 700;
  color: var(--muted);
}

.candidate-row {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 0.5rem;
  padding: 0.5rem 0;
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

.score {
  display: inline-flex;
  padding: 0.15rem 0.4rem;
  border-radius: 999px;
  background: #e6f4eb;
  color: #1a624d;
  font-size: 0.78rem;
  font-weight: 700;
}

.candidate-row a {
  color: var(--pine);
  font-weight: 700;
  font-size: 0.85rem;
  text-decoration: none;
  flex-shrink: 0;
}

.candidate-row a:hover {
  text-decoration: underline;
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

  .card-actions {
    width: 100%;
  }

  .card-actions .action-btn {
    flex: 1;
  }
}
</style>
