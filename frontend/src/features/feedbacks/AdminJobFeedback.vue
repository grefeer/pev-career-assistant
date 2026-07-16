<script setup lang="ts">
import { onMounted, ref } from "vue";
import { fetchAdminFeedbacks } from "./feedbackApi";
import { FEEDBACK_CATEGORY_LABELS } from "./feedbackTypes";
import type { AdminJobFeedback, JobFeedbackCategory } from "./feedbackTypes";

const props = defineProps<{ token: string }>();

const feedbacks = ref<AdminJobFeedback[]>([]);
const loading = ref(false);
const errorMessage = ref("");
const filterJobId = ref("");

async function loadFeedbacks() {
  loading.value = true;
  try {
    const response = await fetchAdminFeedbacks(props.token, filterJobId.value || undefined);
    feedbacks.value = response.feedbacks;
  } catch (err: any) {
    errorMessage.value = err.message || "加载反馈失败";
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  loadFeedbacks();
});
</script>

<template>
  <div class="admin-feedback-panel">
    <div class="section-head">
      <h3>全部职位反馈</h3>
    </div>

    <div v-if="errorMessage" class="banner error">{{ errorMessage }}</div>

    <div class="filter-row">
      <div class="field">
        <label>按职位 ID 筛选</label>
        <input v-model="filterJobId" placeholder="留空显示全部" @input="loadFeedbacks" />
      </div>
      <button class="ghost-button" :disabled="loading" @click="loadFeedbacks">
        {{ loading ? "加载中…" : "刷新" }}
      </button>
    </div>

    <div v-if="loading" class="display-box">加载中…</div>
    <div v-else-if="feedbacks.length === 0" class="display-box">暂无反馈。</div>
    <div v-else class="feedback-list">
      <div v-for="item in feedbacks" :key="item.id" class="feedback-item">
        <div class="feedback-header">
          <strong>{{ FEEDBACK_CATEGORY_LABELS[item.category as JobFeedbackCategory] || item.category }}</strong>
          <span class="feedback-job">{{ item.job_id }}</span>
          <span class="feedback-time">{{ item.created_at }}</span>
        </div>
        <p v-if="item.note" class="feedback-note">{{ item.note }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.admin-feedback-panel {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.section-head h3 {
  margin: 0;
}

.filter-row {
  display: flex;
  gap: 1rem;
  align-items: flex-end;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid rgba(229, 231, 235, 0.9);
  border-radius: 24px;
  padding: 1.5rem;
}

.filter-row .field {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.filter-row .field label {
  font-size: 0.92rem;
  font-weight: 700;
}

.filter-row .field input {
  border: 1px solid #dbe3ea;
  border-radius: 16px;
  padding: 0.85rem 1rem;
  background: #fbfcfd;
  width: 100%;
}

.ghost-button {
  background: #f8fafc;
  color: #0f172a;
  border: 1px solid #dbe3ea;
  border-radius: 16px;
  padding: 0.8rem 1rem;
  cursor: pointer;
  white-space: nowrap;
}

.ghost-button:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: 0 12px 24px rgba(15, 23, 42, 0.08);
}

.banner {
  border-radius: 16px;
  padding: 0.8rem 1rem;
}

.error { background: #fee2e2; color: #991b1b; }

.display-box {
  background: #fafaf9;
  border: 1px solid #e5e7eb;
  border-radius: 16px;
  padding: 1rem;
  color: #6b7280;
  text-align: center;
}

.feedback-list {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.feedback-item {
  background: #fafaf9;
  border: 1px solid #e5e7eb;
  border-radius: 16px;
  padding: 1rem;
}

.feedback-header {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: center;
}

.feedback-job {
  font-size: 0.85rem;
  color: #6b7280;
  font-family: monospace;
}

.feedback-time {
  font-size: 0.8rem;
  color: #9ca3af;
  margin-left: auto;
}

.feedback-note {
  margin: 0.5rem 0 0;
  color: #374151;
  line-height: 1.5;
}
</style>
