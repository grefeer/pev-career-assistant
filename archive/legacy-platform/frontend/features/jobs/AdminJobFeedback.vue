<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ApiError } from "../../api";
import { decideJobFeedback, fetchAdminJobFeedback, generateFeedbackKey } from "./jobFeedbackApi";
import { FEEDBACK_CATEGORY_LABELS } from "./jobFeedbackTypes";
import type {
  AdminFeedbackQueueResponse, FeedbackAdminDecision, JobFeedbackCategory, JobFeedbackStatus,
} from "./jobFeedbackTypes";

const props = defineProps<{ token: string }>();
const queue = ref<AdminFeedbackQueueResponse>({ total: 0, feedback: [], aggregates: [] });
const error = ref("");
const busyId = ref("");
const pending = new Map<string, { fingerprint: string; key: string }>();
const statusFilter = ref<JobFeedbackStatus | "">("");
const categoryFilter = ref<JobFeedbackCategory | "">("");
const limit = 20;
const offset = ref(0);
const page = computed(() => Math.floor(offset.value / limit) + 1);
const pageCount = computed(() => Math.max(1, Math.ceil(queue.value.total / limit)));

function isStaleFeedbackError(caught: unknown): boolean {
  if (!(caught instanceof ApiError) || caught.status !== 409) return false;
  if (!caught.detail || typeof caught.detail !== "object") return false;
  return (caught.detail as Record<string, unknown>).error_code === "stale_job_feedback";
}

async function load() {
  error.value = "";
  try {
    queue.value = await fetchAdminJobFeedback(props.token, {
      status: statusFilter.value || undefined,
      category: categoryFilter.value || undefined,
      limit,
      offset: offset.value,
    });
  }
  catch (caught) { error.value = caught instanceof Error ? caught.message : "反馈队列加载失败。"; }
}
async function applyFilters() {
  offset.value = 0;
  await load();
}
async function previousPage() {
  offset.value = Math.max(0, offset.value - limit);
  await load();
}
async function nextPage() {
  if (offset.value + limit >= queue.value.total) return;
  offset.value += limit;
  await load();
}
async function decide(id: string, version: number, decision: FeedbackAdminDecision) {
  const fingerprint = `${decision}:${version}`;
  const prior = pending.get(id);
  if (!prior || prior.fingerprint !== fingerprint) {
    pending.set(id, { fingerprint, key: generateFeedbackKey() });
  }
  busyId.value = id; error.value = "";
  try {
    await decideJobFeedback(
      props.token, id, { decision, expected_version: version }, pending.get(id)!.key,
    );
    pending.delete(id);
    await load();
  } catch (caught) {
    if (isStaleFeedbackError(caught)) {
      pending.delete(id);
      await load();
    }
    error.value = caught instanceof Error ? caught.message : "处置失败，请重试。";
  }
  finally { busyId.value = ""; }
}
onMounted(load);
</script>

<template>
  <section class="admin-feedback">
    <h2>职位反馈处置</h2>
    <p class="safety-hint">反馈处置不会改变职位状态；失效请前往职位审核页。</p>
    <div class="filters">
      <label>状态
        <select v-model="statusFilter" data-test="status-filter" @change="applyFilters">
          <option value="">全部</option>
          <option value="open">待处理</option>
          <option value="accepted">已接受</option>
          <option value="resolved">已解决</option>
          <option value="rejected">已驳回</option>
          <option value="withdrawn">已撤回</option>
        </select>
      </label>
      <label>类型
        <select v-model="categoryFilter" data-test="category-filter" @change="applyFilters">
          <option value="">全部</option>
          <option v-for="(label, value) in FEEDBACK_CATEGORY_LABELS" :key="value" :value="value">{{ label }}</option>
        </select>
      </label>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <div v-for="aggregate in queue.aggregates" :key="`${aggregate.job_id}:${aggregate.category}`" class="aggregate">
      <strong>{{ aggregate.company_name }} · {{ aggregate.title }}</strong>
      <span>{{ FEEDBACK_CATEGORY_LABELS[aggregate.category] }}：{{ aggregate.total_count }} 条反馈</span>
    </div>
    <article v-for="item in queue.feedback" :key="item.id">
      <h3>{{ item.company_name }} · {{ item.title }}</h3>
      <p>{{ FEEDBACK_CATEGORY_LABELS[item.category] }} · {{ item.status }} · v{{ item.version }}</p>
      <p v-if="item.note">{{ item.note }}</p>
      <div>
        <button v-if="item.status === 'open'" :data-test="`accept-${item.id}`" :disabled="busyId === item.id" @click="decide(item.id, item.version, 'accept')">接受</button>
        <button :data-test="`resolve-${item.id}`" :disabled="busyId === item.id" @click="decide(item.id, item.version, 'resolve')">解决</button>
        <button :data-test="`reject-${item.id}`" :disabled="busyId === item.id" @click="decide(item.id, item.version, 'reject')">驳回</button>
      </div>
    </article>
    <nav class="pagination" aria-label="反馈队列分页">
      <button data-test="previous-page" :disabled="offset === 0" @click="previousPage">上一页</button>
      <span>第 {{ page }} / {{ pageCount }} 页，共 {{ queue.total }} 条</span>
      <button data-test="next-page" :disabled="offset + limit >= queue.total" @click="nextPage">下一页</button>
    </nav>
  </section>
</template>

<style scoped>
.admin-feedback { display: grid; gap: 1rem; } .aggregate, article { padding: 1rem; border: 1px solid #dbe3ea; border-radius: 16px; background: white; }
.safety-hint { color: #475569; } .filters, .pagination { display: flex; align-items: center; gap: 1rem; flex-wrap: wrap; }
.filters label { display: grid; gap: .35rem; } select { padding: .5rem; border: 1px solid #cbd5e1; border-radius: 8px; }
button { margin-right: .5rem; padding: .5rem .8rem; }
</style>
