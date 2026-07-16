<script setup lang="ts">
import {
  computed,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  watch,
} from "vue";

import { ApiError } from "../../api";
import {
  decideJob,
  fetchAdminVerifiedJobs,
  fetchJobReviewQueue,
  saveJobCompletion,
} from "./jobsApi";
import type {
  AdminJobDetail,
  JobCompletionPayload,
  JobSourceCandidate,
  ReviewQueueStatus,
} from "./jobTypes";

const props = defineProps<{ token: string }>();
const emit = defineEmits<{ "dirty-change": [dirty: boolean] }>();

const PAGE_SIZE = 10;
const candidateFields: ReadonlyArray<{
  key: keyof JobSourceCandidate;
  label: string;
}> = [
  { key: "company_name", label: "公司" },
  { key: "title", label: "岗位" },
  { key: "locations", label: "地点" },
  { key: "recruitment_types", label: "招聘类型" },
  { key: "industries", label: "行业" },
  { key: "apply_url", label: "投递入口" },
  { key: "referral_code", label: "内推码" },
  { key: "deadline_text", label: "截止日期" },
];
const rejectReasons = [
  { code: "invalid_source", label: "来源无效" },
  { code: "wrong_company", label: "公司归属错误" },
  { code: "insufficient_job_details", label: "无法形成可信职位" },
  { code: "unsafe_or_invalid_apply_channel", label: "投递渠道无效或不安全" },
] as const;
const expireReasons = [
  { code: "closed_on_official_site", label: "官网已关闭" },
  { code: "deadline_passed", label: "截止日期已过" },
  { code: "application_channel_unavailable", label: "投递渠道不可用" },
] as const;

const mode = ref<"review" | "verified">("review");
const queueJobs = ref<AdminJobDetail[]>([]);
const verifiedJobs = ref<AdminJobDetail[]>([]);
const selected = ref<AdminJobDetail | null>(null);
const queueTotal = ref(0);
const verifiedTotal = ref(0);
const queueOffset = ref(0);
const verifiedOffset = ref(0);
const reviewStatus = ref<"" | ReviewQueueStatus>("");
const listLoading = ref(false);
const actionBusy = ref(false);
const listError = ref("");
const error = ref("");
const message = ref("");
const guiChoice = ref<"" | "yes" | "no">("");
const rejectReason = ref("");
const expireReason = ref("");
const baseline = ref("");
let listRequestVersion = 0;

const form = reactive<JobCompletionPayload>({
  expected_version: 0,
  company_name: "",
  title: "",
  description_text: "",
  locations: [],
  recruitment_types: [],
  industries: [],
  apply_url: "",
  referral_code: null,
  deadline_text: null,
});

function completionSnapshot(): string {
  return JSON.stringify({
    company_name: form.company_name,
    title: form.title,
    description_text: form.description_text,
    locations: form.locations,
    recruitment_types: form.recruitment_types,
    industries: form.industries,
    apply_url: form.apply_url,
    referral_code: form.referral_code,
    deadline_text: form.deadline_text,
  });
}

const dirty = computed(
  () => mode.value === "review" && Boolean(selected.value) && completionSnapshot() !== baseline.value,
);
const manualChannel = computed(() => {
  const value = form.apply_url.trim().toLowerCase();
  return /^(mailto:|weixin:|wechat:|qr:)/.test(value) || /二维码|qrcode|qr-code/.test(value);
});
const canSave = computed(() =>
  Boolean(selected.value && ["pending_completion", "pending_review", "rejected"].includes(selected.value.status)),
);
const canReject = computed(() =>
  Boolean(selected.value && ["pending_completion", "pending_review"].includes(selected.value.status)),
);
const canVerify = computed(() =>
  selected.value?.status === "pending_review"
  && !dirty.value
  && guiChoice.value !== ""
  && !actionBusy.value,
);
const currentJobs = computed(() => mode.value === "review" ? queueJobs.value : verifiedJobs.value);
const currentTotal = computed(() => mode.value === "review" ? queueTotal.value : verifiedTotal.value);
const currentOffset = computed(() => mode.value === "review" ? queueOffset.value : verifiedOffset.value);

watch(dirty, (value) => emit("dirty-change", value), { immediate: true });
watch(manualChannel, (value) => {
  if (value) guiChoice.value = "no";
});

function resetFeedback(): void {
  message.value = "";
  error.value = "";
}

function populateForm(job: AdminJobDetail): void {
  Object.assign(form, {
    expected_version: job.review_version,
    company_name: job.company_name,
    title: job.title,
    description_text: job.description_text || "",
    locations: [...job.locations],
    recruitment_types: [...job.recruitment_types],
    industries: [...job.industries],
    apply_url: job.apply_url,
    referral_code: job.referral_code,
    deadline_text: job.deadline_text,
  });
  guiChoice.value = job.status === "pending_review" && job.gui_eligible ? "yes" : "";
  if (/^(mailto:|weixin:|wechat:|qr:)/i.test(job.apply_url)) guiChoice.value = "no";
  rejectReason.value = "";
  baseline.value = completionSnapshot();
}

function confirmDraftLoss(): boolean {
  return !dirty.value || window.confirm("当前职位有未保存修改，确定放弃并离开吗？");
}

function selectJob(job: AdminJobDetail, force = false): boolean {
  if (!force && selected.value?.id !== job.id && !confirmDraftLoss()) return false;
  selected.value = job;
  if (mode.value === "review") populateForm(job);
  expireReason.value = "";
  return true;
}

function selectAfterLoad(rows: AdminJobDetail[], preferredId?: string): void {
  const next = rows.find((item) => item.id === preferredId) || rows[0] || null;
  if (next) selectJob(next, true);
  else selected.value = null;
}

function listMessage(caught: unknown): string {
  if (caught instanceof ApiError && caught.status === 401) return "登录状态已失效，请重新登录。";
  if (caught instanceof ApiError && caught.status === 403) return "当前账号没有职位审核权限。";
  return "职位列表加载失败，请稍后重试。";
}

async function loadQueue(preferredId?: string, propagate = false): Promise<void> {
  const requestVersion = ++listRequestVersion;
  listLoading.value = true;
  listError.value = "";
  try {
    const response = await fetchJobReviewQueue(props.token, {
      limit: PAGE_SIZE,
      offset: queueOffset.value,
      ...(reviewStatus.value ? { reviewStatus: reviewStatus.value } : {}),
    });
    if (requestVersion !== listRequestVersion) return;
    queueJobs.value = response.jobs;
    queueTotal.value = response.total;
    if (mode.value === "review") selectAfterLoad(response.jobs, preferredId);
  } catch (caught) {
    if (requestVersion !== listRequestVersion) return;
    listError.value = listMessage(caught);
    if (propagate) throw caught;
  } finally {
    if (requestVersion === listRequestVersion) listLoading.value = false;
  }
}

async function loadVerified(preferredId?: string, propagate = false): Promise<void> {
  const requestVersion = ++listRequestVersion;
  listLoading.value = true;
  listError.value = "";
  try {
    const response = await fetchAdminVerifiedJobs(props.token, {
      limit: PAGE_SIZE,
      offset: verifiedOffset.value,
    });
    if (requestVersion !== listRequestVersion) return;
    verifiedJobs.value = response.jobs;
    verifiedTotal.value = response.total;
    if (mode.value === "verified") selectAfterLoad(response.jobs, preferredId);
  } catch (caught) {
    if (requestVersion !== listRequestVersion) return;
    listError.value = listMessage(caught);
    if (propagate) throw caught;
  } finally {
    if (requestVersion === listRequestVersion) listLoading.value = false;
  }
}

async function refreshCurrent(): Promise<void> {
  if (!confirmDraftLoss()) return;
  resetFeedback();
  if (mode.value === "review") await loadQueue(selected.value?.id);
  else await loadVerified(selected.value?.id);
}

async function switchMode(next: "review" | "verified"): Promise<void> {
  if (next === mode.value) {
    await refreshCurrent();
    return;
  }
  if (!confirmDraftLoss()) return;
  mode.value = next;
  selected.value = null;
  resetFeedback();
  if (next === "review") await loadQueue();
  else await loadVerified();
}

async function changePage(direction: -1 | 1): Promise<void> {
  if (!confirmDraftLoss()) return;
  if (mode.value === "review") {
    queueOffset.value = Math.max(0, queueOffset.value + direction * PAGE_SIZE);
    await loadQueue();
  } else {
    verifiedOffset.value = Math.max(0, verifiedOffset.value + direction * PAGE_SIZE);
    await loadVerified();
  }
}

async function changeStatus(): Promise<void> {
  if (!confirmDraftLoss()) return;
  queueOffset.value = 0;
  await loadQueue();
}

function setListField(
  key: "locations" | "recruitment_types" | "industries",
  event: Event,
): void {
  const value = (event.target as HTMLInputElement).value;
  form[key] = value.split(/[，,]/).map((item) => item.trim()).filter(Boolean);
}

function candidateValue(key: keyof JobSourceCandidate): string {
  const value = selected.value?.source_candidate[key];
  if (Array.isArray(value)) return value.join("、") || "—";
  return value == null || value === "" ? "—" : String(value);
}

function apiErrorMessage(caught: unknown): string {
  if (!(caught instanceof ApiError)) return "职位审核操作失败，请稍后重试。";
  if (caught.status === 401) return "登录状态已失效，请重新登录。";
  if (caught.status === 403) return "当前账号没有职位审核权限。";
  if (caught.status === 422) return "职位信息未通过校验，请检查必填字段。";
  const detail = caught.detail;
  const code = detail && typeof detail === "object"
    ? (detail as Record<string, unknown>).error_code
    : null;
  if (caught.status === 409 && code === "invalid_job_transition") {
    return "职位状态已变化，当前操作不再允许。";
  }
  return "职位审核操作失败，请稍后重试。";
}

function isExactStale(caught: unknown): boolean {
  if (!(caught instanceof ApiError) || caught.status !== 409) return false;
  return Boolean(
    caught.detail
    && typeof caught.detail === "object"
    && (caught.detail as Record<string, unknown>).error_code === "stale_job_review",
  );
}

async function handleActionError(
  caught: unknown,
  targetMode: "review" | "verified",
  targetId: string,
): Promise<void> {
  if (!isExactStale(caught)) {
    error.value = apiErrorMessage(caught);
    return;
  }
  try {
    const preferredId = selected.value?.id || targetId;
    if (targetMode === "review") await loadQueue(preferredId, true);
    else await loadVerified(preferredId, true);
    error.value = "职位已被其他审核人更新，请重新检查。";
  } catch {
    error.value = "职位已被其他审核人更新，但重新加载失败，请手动刷新后再操作。";
  }
}

async function save(): Promise<void> {
  if (!selected.value || !canSave.value || actionBusy.value) return;
  const targetId = selected.value.id;
  const targetVersion = selected.value.review_version;
  const payload: JobCompletionPayload = {
    ...form,
    expected_version: targetVersion,
    locations: [...form.locations],
    recruitment_types: [...form.recruitment_types],
    industries: [...form.industries],
    referral_code: form.referral_code?.trim() || null,
    deadline_text: form.deadline_text?.trim() || null,
  };
  resetFeedback();
  actionBusy.value = true;
  try {
    const updated = await saveJobCompletion(props.token, targetId, payload);
    queueJobs.value = queueJobs.value.map((item) => item.id === targetId ? updated : item);
    if (selected.value?.id === targetId && selected.value.review_version === targetVersion) {
      selected.value = updated;
      populateForm(updated);
    }
    message.value = "补全草稿已保存。";
  } catch (caught) {
    await handleActionError(caught, "review", targetId);
  } finally {
    actionBusy.value = false;
  }
}

async function verify(): Promise<void> {
  if (!selected.value || !canVerify.value || actionBusy.value) return;
  const targetId = selected.value.id;
  const targetVersion = selected.value.review_version;
  actionBusy.value = true;
  resetFeedback();
  try {
    await decideJob(props.token, targetId, {
      expected_version: targetVersion,
      decision: "verify",
      gui_eligible: !manualChannel.value && guiChoice.value === "yes",
      reason_code: null,
    });
    message.value = "职位已核验并发布。";
    await loadQueue(selected.value?.id);
  } catch (caught) {
    await handleActionError(caught, "review", targetId);
  } finally {
    actionBusy.value = false;
  }
}

async function reject(): Promise<void> {
  if (!selected.value || !canReject.value || !rejectReason.value || actionBusy.value) return;
  const targetId = selected.value.id;
  const targetVersion = selected.value.review_version;
  actionBusy.value = true;
  resetFeedback();
  try {
    await decideJob(props.token, targetId, {
      expected_version: targetVersion,
      decision: "reject",
      gui_eligible: false,
      reason_code: rejectReason.value,
    });
    message.value = "职位记录已拒绝。";
    await loadQueue(selected.value?.id);
  } catch (caught) {
    await handleActionError(caught, "review", targetId);
  } finally {
    actionBusy.value = false;
  }
}

async function expire(): Promise<void> {
  if (!selected.value || selected.value.status !== "verified" || !expireReason.value || actionBusy.value) return;
  const targetId = selected.value.id;
  const targetVersion = selected.value.review_version;
  actionBusy.value = true;
  resetFeedback();
  try {
    await decideJob(props.token, targetId, {
      expected_version: targetVersion,
      decision: "expire",
      gui_eligible: false,
      reason_code: expireReason.value,
    });
    message.value = "职位已标记失效。";
    await loadVerified(selected.value?.id);
  } catch (caught) {
    await handleActionError(caught, "verified", targetId);
  } finally {
    actionBusy.value = false;
  }
}

function beforeUnload(event: BeforeUnloadEvent): void {
  if (!dirty.value) return;
  event.preventDefault();
  event.returnValue = "";
}

onMounted(() => {
  window.addEventListener("beforeunload", beforeUnload);
  void loadQueue();
});
onBeforeUnmount(() => {
  ++listRequestVersion;
  window.removeEventListener("beforeunload", beforeUnload);
  emit("dirty-change", false);
});
</script>

<template>
  <section class="admin-job-review" aria-labelledby="review-title">
    <header class="review-header">
      <div>
        <p class="kicker">ADMIN · JOB INTEGRITY</p>
        <h1 id="review-title">职位审核台</h1>
        <p>对照来源候选值，补全可信字段，再执行明确的生命周期决策。</p>
      </div>
      <div class="mode-switch" aria-label="审核列表">
        <button
          type="button"
          data-test="review-queue-tab"
          :class="{ active: mode === 'review' }"
          @click="switchMode('review')"
        >待处理</button>
        <button
          type="button"
          data-test="verified-tab"
          :class="{ active: mode === 'verified' }"
          @click="switchMode('verified')"
        >已核验生命周期</button>
      </div>
    </header>

    <div class="review-toolbar">
      <label v-if="mode === 'review'">
        状态
        <select v-model="reviewStatus" data-test="status-filter" @change="changeStatus">
          <option value="">全部非终态</option>
          <option value="pending_completion">待补全</option>
          <option value="pending_review">待核验</option>
          <option value="rejected">已拒绝（可重开）</option>
        </select>
      </label>
      <span>共 {{ currentTotal }} 条 · 第 {{ Math.floor(currentOffset / PAGE_SIZE) + 1 }} 页</span>
      <button type="button" data-test="refresh-queue" :disabled="listLoading" @click="refreshCurrent">
        {{ listLoading ? "载入中…" : "刷新" }}
      </button>
    </div>

    <p v-if="listError" class="notice error" role="alert">{{ listError }}</p>
    <p v-if="message" class="notice success" role="status">{{ message }}</p>
    <p v-if="error" class="notice error" role="alert">{{ error }}</p>

    <div class="review-layout">
      <aside class="queue-panel" aria-label="职位列表">
        <div v-if="listLoading && currentJobs.length === 0" class="empty-state">正在读取职位…</div>
        <div v-else-if="currentJobs.length === 0" class="empty-state">
          {{ mode === "review" ? "当前筛选没有待处理职位。" : "当前没有已核验职位。" }}
        </div>
        <button
          v-for="item in currentJobs"
          :key="item.id"
          type="button"
          class="queue-item"
          :class="{ selected: selected?.id === item.id }"
          :data-test="`queue-job-${item.id}`"
          @click="selectJob(item)"
        >
          <span class="status-mark">{{ item.status }}</span>
          <strong>{{ item.company_name }}</strong>
          <span>{{ item.title }}</span>
          <small>v{{ item.review_version }} · {{ item.source_name }}</small>
        </button>
        <div class="pager">
          <button
            type="button"
            data-test="previous-page"
            :disabled="currentOffset === 0 || listLoading"
            @click="changePage(-1)"
          >上一页</button>
          <button
            type="button"
            data-test="next-page"
            :disabled="currentOffset + PAGE_SIZE >= currentTotal || listLoading"
            @click="changePage(1)"
          >下一页</button>
        </div>
      </aside>

      <main v-if="selected && mode === 'review'" class="editor-panel">
        <div class="editor-heading">
          <div>
            <span class="status-mark">{{ selected.status }}</span>
            <h2>{{ selected.company_name }} · {{ selected.title }}</h2>
          </div>
          <span v-if="dirty" class="dirty-badge">有未保存修改</span>
        </div>

        <p v-if="selected.source_changed_since_review" class="source-warning" role="alert">
          来源数据已变化，请对照候选值重新审核。
        </p>

        <section class="candidate-board" aria-label="最新来源候选值">
          <h3>八字段来源对照</h3>
          <dl>
            <div
              v-for="candidate in candidateFields"
              :key="candidate.key"
              :data-test="`candidate-${candidate.key}`"
            >
              <dt>{{ candidate.label }}</dt>
              <dd>{{ candidateValue(candidate.key) }}</dd>
            </div>
          </dl>
        </section>

        <form class="completion-form" @submit.prevent="save">
          <label>公司<input v-model="form.company_name" data-test="company" /></label>
          <label>岗位<input v-model="form.title" data-test="title" /></label>
          <label class="wide">完整 JD<textarea v-model="form.description_text" data-test="description" rows="8" /></label>
          <label>地点<input :value="form.locations.join('，')" data-test="locations" @input="setListField('locations', $event)" /></label>
          <label>招聘类型<input :value="form.recruitment_types.join('，')" data-test="recruitment-types" @input="setListField('recruitment_types', $event)" /></label>
          <label>行业<input :value="form.industries.join('，')" data-test="industries" @input="setListField('industries', $event)" /></label>
          <label>投递入口<input v-model="form.apply_url" data-test="apply-url" /></label>
          <label>内推码<input v-model="form.referral_code" data-test="referral-code" /></label>
          <label>截止日期<input v-model="form.deadline_text" data-test="deadline" /></label>

          <fieldset v-if="selected.status === 'pending_review'" class="wide gui-choice">
            <legend>是否允许 GUI 辅助填写</legend>
            <label><input v-model="guiChoice" type="radio" value="yes" :disabled="manualChannel" />允许</label>
            <label><input v-model="guiChoice" type="radio" value="no" />仅人工投递</label>
            <small v-if="manualChannel">邮箱、二维码等人工渠道已强制关闭 GUI 辅助。</small>
          </fieldset>

          <div class="action-bar wide">
            <button
              v-if="canSave"
              data-test="save-completion"
              type="button"
              :disabled="actionBusy"
              @click="save"
            >{{ actionBusy ? "处理中…" : "保存补全草稿" }}</button>
            <button
              v-if="selected.status === 'pending_review'"
              data-test="verify-job"
              type="button"
              class="primary-action"
              :disabled="!canVerify"
              @click="verify"
            >核验并发布</button>
          </div>
        </form>

        <section v-if="canReject" class="decision-box">
          <label>
            拒绝原因
            <select v-model="rejectReason" data-test="reject-reason">
              <option value="">请选择稳定原因码</option>
              <option v-for="reason in rejectReasons" :key="reason.code" :value="reason.code">
                {{ reason.label }} · {{ reason.code }}
              </option>
            </select>
          </label>
          <button
            type="button"
            data-test="reject-job"
            :disabled="!rejectReason || actionBusy"
            @click="reject"
          >拒绝记录</button>
        </section>
      </main>

      <main v-else-if="selected" class="editor-panel lifecycle-panel">
        <span class="status-mark">verified · v{{ selected.review_version }}</span>
        <h2>{{ selected.company_name }} · {{ selected.title }}</h2>
        <p>{{ selected.locations.join("、") }} · {{ selected.apply_url }}</p>
        <section class="decision-box">
          <label>
            失效原因
            <select v-model="expireReason" data-test="expire-reason">
              <option value="">请选择稳定原因码</option>
              <option v-for="reason in expireReasons" :key="reason.code" :value="reason.code">
                {{ reason.label }} · {{ reason.code }}
              </option>
            </select>
          </label>
          <button
            type="button"
            data-test="expire-job"
            :disabled="!expireReason || actionBusy"
            @click="expire"
          >标记职位失效</button>
        </section>
      </main>

      <main v-else class="editor-panel empty-state">选择左侧职位开始审核。</main>
    </div>
  </section>
</template>

<style scoped>
.admin-job-review {
  --ink: #17372d;
  --muted: #6e7c75;
  --paper: #fffdf8;
  --line: #d8ddd6;
  --green: #17634e;
  --amber: #d98b24;
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
.review-toolbar,
.editor-heading,
.action-bar,
.decision-box,
.pager,
.mode-switch {
  display: flex;
  align-items: center;
  gap: 0.75rem;
}

.review-header {
  justify-content: space-between;
  margin-bottom: 1rem;
}

.review-header h1,
.editor-heading h2,
.lifecycle-panel h2 {
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
.mode-switch .active,
.primary-action { color: white; background: var(--green); }

.review-toolbar {
  justify-content: flex-end;
  padding: 0.75rem;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
}
.review-toolbar label { margin-right: auto; }
.review-toolbar select { margin-left: 0.5rem; }

.review-layout {
  display: grid;
  grid-template-columns: minmax(220px, 0.32fr) minmax(0, 1fr);
  gap: 1rem;
  margin-top: 1rem;
}

.queue-panel,
.editor-panel {
  border: 1px solid var(--line);
  border-radius: 18px;
  background: rgba(255, 255, 255, 0.8);
}

.queue-panel { padding: 0.55rem; }
.queue-item { width: 100%; margin-bottom: 0.45rem; text-align: left; display: grid; gap: 0.25rem; }
.queue-item.selected { border-color: var(--green); box-shadow: inset 4px 0 var(--green); }
.queue-item small,
.queue-item > span:not(.status-mark) { color: var(--muted); }
.status-mark,
.dirty-badge {
  width: fit-content;
  padding: 0.18rem 0.45rem;
  border-radius: 999px;
  color: var(--green);
  background: #e2f1e9;
  font: 700 0.72rem/1.4 ui-monospace, monospace;
}
.dirty-badge { color: #8a4d00; background: #fff0d6; }
.pager { justify-content: space-between; margin-top: 0.65rem; }

.editor-panel { min-width: 0; padding: clamp(0.9rem, 2vw, 1.4rem); }
.editor-heading { justify-content: space-between; }
.source-warning,
.notice { padding: 0.75rem 0.9rem; border-radius: 10px; }
.source-warning { color: #7a4300; background: #fff3d7; border-left: 4px solid var(--amber); }
.notice.error { color: #8c2828; background: #ffebeb; }
.notice.success { color: #185c43; background: #e5f5ed; }

.candidate-board { margin: 1rem 0; padding: 1rem; border: 1px dashed #b9c8bd; border-radius: 14px; background: #f4f7f2; }
.candidate-board h3 { margin-top: 0; }
.candidate-board dl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.65rem; margin: 0; }
.candidate-board dl > div { min-width: 0; padding: 0.6rem; background: white; border-radius: 8px; }
.candidate-board dt { color: var(--muted); font-size: 0.78rem; }
.candidate-board dd { margin: 0.25rem 0 0; overflow-wrap: anywhere; }

.completion-form { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.8rem; }
.completion-form > label,
.decision-box label { display: grid; gap: 0.35rem; font-weight: 700; }
.completion-form input,
.completion-form textarea,
.decision-box select,
.review-toolbar select {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid #cbd4cc;
  border-radius: 10px;
  padding: 0.7rem;
  color: var(--ink);
  background: white;
}
.wide { grid-column: 1 / -1; }
.gui-choice { display: flex; flex-wrap: wrap; gap: 1rem; border: 1px solid var(--line); border-radius: 12px; }
.gui-choice small { width: 100%; color: #8a4d00; }
.action-bar { justify-content: flex-end; }
.decision-box { justify-content: space-between; margin-top: 1rem; padding: 1rem; border-radius: 12px; background: #f7f4ed; }
.decision-box label { flex: 1; }
.empty-state { padding: 2rem 1rem; color: var(--muted); text-align: center; }

@media (max-width: 900px) {
  .review-header,
  .review-toolbar { align-items: stretch; flex-direction: column; }
  .review-toolbar label { margin-right: 0; }
  .review-layout { grid-template-columns: 1fr; }
  .queue-panel { max-height: 340px; overflow: auto; }
}

@media (max-width: 600px) {
  .completion-form,
  .candidate-board dl { grid-template-columns: 1fr; }
  .wide { grid-column: auto; }
  .decision-box { align-items: stretch; flex-direction: column; }
}
</style>
