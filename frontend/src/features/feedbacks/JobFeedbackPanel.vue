<script setup lang="ts">
import { onMounted, ref } from "vue";
import {
  createFeedback,
  fetchFeedbacks,
} from "./feedbackApi";
import { FEEDBACK_CATEGORY_LABELS } from "./feedbackTypes";
import type { JobFeedback, JobFeedbackCategory } from "./feedbackTypes";

const props = defineProps<{ token: string }>();

const feedbacks = ref<JobFeedback[]>([]);
const loading = ref(false);
const submitting = ref(false);
const errorMessage = ref("");
const successMessage = ref("");

const selectedJobId = ref("");
const category = ref<JobFeedbackCategory>("closed");
const note = ref("");

async function loadFeedbacks() {
  loading.value = true;
  try {
    const response = await fetchFeedbacks(props.token, selectedJobId.value || undefined);
    feedbacks.value = response.feedbacks;
  } catch (err: any) {
    errorMessage.value = err.message || "加载反馈失败";
  } finally {
    loading.value = false;
  }
}

async function handleSubmit() {
  if (!selectedJobId.value) {
    errorMessage.value = "请输入职位 ID。";
    return;
  }
  submitting.value = true;
  errorMessage.value = "";
  successMessage.value = "";
  try {
    await createFeedback(props.token, {
      job_id: selectedJobId.value,
      category: category.value,
      note: note.value || undefined,
    });
    successMessage.value = "反馈已提交。";
    note.value = "";
    await loadFeedbacks();
  } catch (err: any) {
    errorMessage.value = err.message || "提交反馈失败";
  } finally {
    submitting.value = false;
  }
}

onMounted(() => {
  loadFeedbacks();
});
</script>

<template>
  <div class="feedback-panel">
    <div class="section-head">
      <h3>职位反馈</h3>
    </div>

    <div v-if="successMessage" class="banner success">{{ successMessage }}</div>
    <div v-if="errorMessage" class="banner error">{{ errorMessage }}</div>

    <div class="feedback-form">
      <div class="field">
        <label>职位 ID</label>
        <input v-model="selectedJobId" placeholder="输入职位 ID 以提交反馈" />
      </div>

      <div class="field">
        <label>反馈类型</label>
        <select v-model="category">
          <option v-for="(label, key) in FEEDBACK_CATEGORY_LABELS" :key="key" :value="key">
            {{ label }}
          </option>
        </select>
      </div>

      <div class="field">
        <label>备注（可选）</label>
        <textarea v-model="note" rows="3" placeholder="补充说明…"></textarea>
      </div>

      <button class="primary-button" :disabled="submitting || !selectedJobId" @click="handleSubmit">
        {{ submitting ? "提交中…" : "提交反馈" }}
      </button>
    </div>

    <div class="feedback-list">
      <h4>我的反馈记录</h4>
      <div v-if="loading" class="display-box">加载中…</div>
      <div v-else-if="feedbacks.length === 0" class="display-box">暂无反馈记录。</div>
      <div v-else>
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
  </div>
</template>

<style scoped>
.feedback-panel {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.section-head h3 {
  margin: 0;
}

.feedback-form {
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid rgba(229, 231, 235, 0.9);
  border-radius: 24px;
  padding: 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.field label {
  font-size: 0.92rem;
  font-weight: 700;
}

.field input,
.field textarea,
.field select {
  width: 100%;
  border: 1px solid #dbe3ea;
  border-radius: 16px;
  padding: 0.85rem 1rem;
  background: #fbfcfd;
}

.primary-button {
  background: linear-gradient(135deg, #0f766e, #14b8a6);
  color: white;
  border: none;
  border-radius: 16px;
  padding: 0.8rem 1rem;
  cursor: pointer;
  width: 100%;
  transition: transform 0.15s ease, box-shadow 0.15s ease;
}

.primary-button:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: 0 12px 24px rgba(15, 23, 42, 0.08);
}

.primary-button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.banner {
  border-radius: 16px;
  padding: 0.8rem 1rem;
}

.success { background: #dcfce7; color: #166534; }
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

.feedback-list h4 {
  margin: 0.5rem 0 0;
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
