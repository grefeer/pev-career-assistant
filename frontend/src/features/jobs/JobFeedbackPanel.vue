<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import { ApiError } from "../../api";
import { fetchMyJobFeedback, generateFeedbackKey, mutateJobFeedback } from "./jobFeedbackApi";
import { FEEDBACK_CATEGORY_LABELS } from "./jobFeedbackTypes";
import type { FeedbackMutationRequest, JobFeedbackCategory, StudentFeedbackItem } from "./jobFeedbackTypes";

const props = defineProps<{ token: string; jobId: string }>();
const feedback = ref<StudentFeedbackItem[]>([]);
const category = ref<JobFeedbackCategory>("closed");
const note = ref("");
const loading = ref(false);
const busy = ref(false);
const message = ref("");
const error = ref("");
const pending = ref<{ fingerprint: string; key: string } | null>(null);
const selected = computed(() => feedback.value.find((item) => item.category === category.value));

function isStaleFeedbackError(caught: unknown): boolean {
  if (!(caught instanceof ApiError) || caught.status !== 409) return false;
  if (!caught.detail || typeof caught.detail !== "object") return false;
  return (caught.detail as Record<string, unknown>).error_code === "stale_job_feedback";
}

async function load() {
  loading.value = true;
  error.value = "";
  try { feedback.value = (await fetchMyJobFeedback(props.token, props.jobId)).feedback; }
  catch (caught) { error.value = caught instanceof Error ? caught.message : "反馈加载失败。"; }
  finally { loading.value = false; }
}

async function perform(payload: FeedbackMutationRequest) {
  const fingerprint = JSON.stringify(payload);
  if (!pending.value || pending.value.fingerprint !== fingerprint) {
    pending.value = { fingerprint, key: generateFeedbackKey() };
  }
  busy.value = true; error.value = ""; message.value = "";
  try {
    await mutateJobFeedback(props.token, props.jobId, payload, pending.value.key);
    pending.value = null;
    message.value = payload.action === "withdraw" ? "反馈已撤回。" : "反馈已保存。";
    await load();
  } catch (caught) {
    if (isStaleFeedbackError(caught)) {
      pending.value = null;
      await load();
    }
    error.value = caught instanceof Error ? caught.message : "反馈保存失败，请重试。";
  } finally { busy.value = false; }
}

function submit() {
  return perform({
    action: "upsert", category: category.value,
    expected_version: selected.value?.version ?? null, note: note.value.trim() || null,
  });
}
function withdraw(item: StudentFeedbackItem) {
  return perform({ action: "withdraw", category: item.category, expected_version: item.version, note: null });
}
watch(() => props.jobId, () => { pending.value = null; feedback.value = []; void load(); });
watch(category, () => { note.value = selected.value?.note ?? ""; pending.value = null; });
onMounted(load);
</script>

<template>
  <section class="feedback-panel" aria-label="我的职位反馈">
    <h4>反馈此职位</h4>
    <p v-if="loading">正在加载反馈…</p>
    <p v-if="message" class="success">{{ message }}</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <label>反馈类型
      <select v-model="category">
        <option v-for="(label, value) in FEEDBACK_CATEGORY_LABELS" :key="value" :value="value">{{ label }}</option>
      </select>
    </label>
    <label>说明（可选）<textarea v-model="note" maxlength="1000" rows="3" /></label>
    <button data-test="feedback-submit" type="button" :disabled="busy" @click="submit">
      {{ selected ? "更新反馈" : "提交反馈" }}
    </button>
    <ul v-if="feedback.length">
      <li v-for="item in feedback" :key="item.id">
        <strong>{{ FEEDBACK_CATEGORY_LABELS[item.category] }}</strong>
        <span>{{ item.status }} · v{{ item.version }}</span>
        <p v-if="item.note">{{ item.note }}</p>
        <button
          v-if="item.status === 'open' || item.status === 'accepted'"
          :data-test="`feedback-withdraw-${item.id}`" type="button" :disabled="busy"
          @click="withdraw(item)"
        >撤回</button>
      </li>
    </ul>
  </section>
</template>

<style scoped>
.feedback-panel { margin-top: 1rem; padding: 1rem; border: 1px solid #dce5df; border-radius: 16px; display: grid; gap: .75rem; }
label { display: grid; gap: .35rem; } select, textarea { padding: .65rem; border: 1px solid #cbd5e1; border-radius: 10px; }
button { width: fit-content; padding: .55rem .9rem; border: 0; border-radius: 10px; background: #1c6650; color: white; }
ul { list-style: none; padding: 0; display: grid; gap: .5rem; } li { padding: .75rem; background: #f7faf8; border-radius: 10px; }
.success { color: #166534; } .error { color: #b91c1c; }
</style>
