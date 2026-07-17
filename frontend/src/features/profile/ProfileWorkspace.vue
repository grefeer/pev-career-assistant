<script setup lang="ts">
import { ref, computed, onMounted, watch } from "vue";
import * as profileApi from "./profileApi";
import type {
  ProfileDetail,
  ResumeAssetMetadata,
  ProfileEvidence,
  ConfirmedProfileVersionSummary,
} from "./profileTypes";
import { ApiError } from "../../api";

const props = defineProps<{ token: string }>();
const emit = defineEmits<{ "dirty-change": [value: boolean] }>();

const profile = ref<ProfileDetail | null>(null);
const assets = ref<ResumeAssetMetadata[]>([]);
const versions = ref<ConfirmedProfileVersionSummary[]>([]);
const loading = ref(false);
const errorMessage = ref("");
const successMessage = ref("");
const selectedImportId = ref<string | null>(null);

// Track local decisions per evidence ID
const localDecisions = ref<Map<string, "confirm" | "ignore" | "correct">>(new Map());
const correctionValues = ref<Map<string, string>>(new Map());
const fileInput = ref<HTMLInputElement | null>(null);

const selectedEvidence = computed(() =>
  profile.value?.evidence.filter(
    (evidence) => evidence.resume_import_id === selectedImportId.value,
  ) ?? [],
);

const allDecided = computed(() => {
  if (selectedEvidence.value.length === 0 || localDecisions.value.size > 0) return false;
  return selectedEvidence.value.every((evidence) => evidence.status !== "pending");
});

const hasLocalChanges = computed(() => localDecisions.value.size > 0);
const canSaveDecisions = computed(() =>
  Array.from(localDecisions.value.entries()).every(
    ([evidenceId, action]) =>
      action !== "correct" || Boolean(correctionValues.value.get(evidenceId)?.trim()),
  ) && hasLocalChanges.value,
);

watch(hasLocalChanges, (val) => {
  emit("dirty-change", val);
});

async function loadProfile() {
  try {
    loading.value = true;
    const [profileData, assetsData, versionsData] = await Promise.all([
      profileApi.fetchProfile(props.token),
      profileApi.fetchResumeAssets(props.token),
      profileApi.fetchProfileVersions(props.token),
    ]);
    profile.value = profileData;
    assets.value = assetsData.assets;
    versions.value = versionsData.versions;
    if (profileData.evidence.length > 0) {
      selectedImportId.value = profileData.evidence[0].resume_import_id;
    }
  } catch (error: any) {
    errorMessage.value = error.message || "加载失败";
  } finally {
    loading.value = false;
  }
}

async function handleUpload() {
  const file = fileInput.value?.files?.[0];
  if (!file) return;
  try {
    loading.value = true;
    const asset = await profileApi.uploadResumeAsset(props.token, file);
    await profileApi.reconcileResumeAsset(props.token, asset.id);
    successMessage.value = "上传成功";
    await loadProfile();
  } catch (error: any) {
    errorMessage.value = error.message || "上传失败";
  } finally {
    if (fileInput.value) fileInput.value.value = "";
    loading.value = false;
  }
}

async function handleReconcile(assetId: string) {
  try {
    loading.value = true;
    await profileApi.reconcileResumeAsset(props.token, assetId);
    successMessage.value = "资产已同步";
    await loadProfile();
  } catch (error: any) {
    errorMessage.value = error.message || "同步失败";
  } finally {
    loading.value = false;
  }
}

async function handleStartImport(assetId: string) {
  try {
    loading.value = true;
    const result = await profileApi.startResumeImport(props.token, assetId);
    selectedImportId.value = result.id;
    successMessage.value = "解析完成";
    await loadProfile();
  } catch (error: any) {
    errorMessage.value = error.message || "解析失败";
  } finally {
    loading.value = false;
  }
}

function setDecision(evidenceId: string, action: "confirm" | "ignore" | "correct") {
  const newMap = new Map(localDecisions.value);
  if (newMap.has(evidenceId) && newMap.get(evidenceId) === action) {
    newMap.delete(evidenceId);
  } else {
    newMap.set(evidenceId, action);
  }
  localDecisions.value = newMap;
}

function setCorrection(evidenceId: string, value: string) {
  const newMap = new Map(correctionValues.value);
  newMap.set(evidenceId, value);
  correctionValues.value = newMap;
}

function parseCorrection(value: string): unknown {
  try {
    return JSON.parse(value);
  } catch {
    return value;
  }
}

async function handleSaveDecisions() {
  if (!profile.value) return;
  try {
    loading.value = true;
    const decisions = Array.from(localDecisions.value.entries()).map(
      ([evidenceId, action]) => ({
        evidence_id: evidenceId,
        action,
        corrected_value:
          action === "correct"
            ? parseCorrection(correctionValues.value.get(evidenceId) ?? "")
            : null,
      }),
    );
    const result = await profileApi.applyEvidenceDecisions(
      props.token,
      profile.value.version,
      decisions as any,
    );
    profile.value.version = result.version;
    localDecisions.value = new Map();
    correctionValues.value = new Map();
    successMessage.value = "校对已保存";
    await loadProfile();
  } catch (error: any) {
    if (error instanceof ApiError && error.status === 409) {
      errorMessage.value = "档案已被其他操作更新，请重新检查差异。";
      localDecisions.value = new Map();
      await loadProfile();
    } else {
      errorMessage.value = error.message || "保存失败";
    }
  } finally {
    loading.value = false;
  }
}

async function handleCreateVersion() {
  if (!profile.value || !selectedImportId.value) return;
  try {
    loading.value = true;
    const result = await profileApi.createProfileVersion(
      props.token,
      profile.value.version,
      selectedImportId.value,
    );
    profile.value.version = result.aggregate_version;
    localDecisions.value = new Map();
    successMessage.value = `版本 ${result.version_number} 已确认`;
    await loadProfile();
  } catch (error: any) {
    if (error instanceof ApiError && error.status === 409) {
      errorMessage.value = "档案已被其他操作更新，请重新检查差异。";
      await loadProfile();
    } else {
      errorMessage.value = error.message || "创建版本失败";
    }
  } finally {
    loading.value = false;
  }
}

function statusLabel(ev: ProfileEvidence): string {
  if (ev.status !== "pending") {
    const labels: Record<string, string> = {
      confirmed: "已确认",
      corrected: "已更正",
      ignored: "已忽略",
    };
    return labels[ev.status] || ev.status;
  }
  return "待处理";
}

onMounted(() => {
  loadProfile();
});
</script>

<template>
  <div class="profile-workspace">
    <header class="workspace-header">
      <div>
        <p class="eyebrow">档案管理</p>
        <h1>简历与档案</h1>
      </div>
    </header>

    <div v-if="successMessage" class="banner success">{{ successMessage }}</div>
    <div v-if="errorMessage" class="banner error">{{ errorMessage }}</div>

    <section class="panel">
      <div class="section-head">
        <h2>上传简历</h2>
      </div>
      <div class="field">
        <label>选择文件（支持 .txt .md .pdf .docx，最大 10 MiB）</label>
        <input
          ref="fileInput"
          type="file"
          accept=".txt,.md,.pdf,.docx"
          @change="handleUpload"
        />
      </div>
    </section>

    <section class="panel">
      <div class="section-head">
        <h2>简历资产</h2>
      </div>
      <div v-if="assets.length === 0" class="display-box">暂无简历资产。</div>
      <div v-else class="asset-list">
        <div v-for="asset in assets" :key="asset.id" class="asset-item">
          <div class="asset-info">
            <strong>{{ asset.original_filename }}</strong>
            <span>{{ asset.content_type }} / {{ asset.plaintext_size }} bytes</span>
            <span>状态：{{ asset.status }}</span>
            <span v-if="asset.error_code" class="error-text">{{ asset.error_code }}</span>
          </div>
          <div class="asset-actions">
            <button
              class="ghost-button"
              :disabled="loading"
              @click="handleReconcile(asset.id)"
            >
              同步
            </button>
            <button
              class="ghost-button"
              :disabled="loading || asset.status !== 'ready'"
              @click="handleStartImport(asset.id)"
            >
              解析
            </button>
          </div>
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="section-head">
        <h2>证据校对</h2>
        <button
          class="primary-button"
          data-test="save-decisions"
          :disabled="loading || !canSaveDecisions"
          @click="handleSaveDecisions"
        >
          保存校对
        </button>
        <button
          class="ghost-button"
          data-test="create-version"
          :disabled="loading || !allDecided || !selectedImportId"
          @click="handleCreateVersion"
        >
          创建确认版本
        </button>
      </div>
      <div v-if="!profile || selectedEvidence.length === 0" class="display-box">
        暂无证据。请先上传并解析简历。
      </div>
      <div v-else class="evidence-table">
        <div
          v-for="ev in selectedEvidence"
          :key="ev.id"
          class="evidence-row"
        >
          <div class="evidence-meta">
            <strong>{{ ev.field_path }}</strong>
            <span class="confidence-badge">置信度 {{ ev.confidence }}%</span>
            <span v-if="ev.diff_action" class="diff-badge" :class="ev.diff_action">
              {{ ev.diff_action === 'add' ? '新增' : ev.diff_action === 'replace' ? '替换' : ev.diff_action === 'unchanged' ? '不变' : '冲突' }}
            </span>
          </div>
          <div class="evidence-excerpt">{{ ev.evidence_excerpt }}</div>
          <div class="evidence-status">{{ statusLabel(ev) }}</div>
          <div class="evidence-decisions">
            <button
              :data-test="`decision-confirm-${ev.id}`"
              class="decision-btn"
              :class="{ active: localDecisions.get(ev.id) === 'confirm' }"
              :disabled="loading"
              @click="setDecision(ev.id, 'confirm')"
            >
              确认
            </button>
            <button
              class="decision-btn"
              :class="{ active: localDecisions.get(ev.id) === 'ignore' }"
              :disabled="loading"
              @click="setDecision(ev.id, 'ignore')"
            >
              忽略
            </button>
            <button
              :data-test="`decision-correct-${ev.id}`"
              class="decision-btn"
              :class="{ active: localDecisions.get(ev.id) === 'correct' }"
              :disabled="loading"
              @click="setDecision(ev.id, 'correct')"
            >
              更正
            </button>
          </div>
          <textarea
            v-if="localDecisions.get(ev.id) === 'correct'"
            :data-test="`correction-${ev.id}`"
            :value="correctionValues.get(ev.id) ?? ''"
            placeholder="输入更正后的 JSON 或文本"
            @input="setCorrection(ev.id, ($event.target as HTMLTextAreaElement).value)"
          />
        </div>
      </div>
    </section>

    <section class="panel">
      <div class="section-head">
        <h2>已确认版本</h2>
      </div>
      <div v-if="versions.length === 0" class="display-box">暂无确认版本。</div>
      <div v-else class="version-list">
        <div v-for="v in versions" :key="v.id" class="version-item">
          <strong>版本 {{ v.version_number }}</strong>
          <span>聚合版本 {{ v.aggregate_version }}</span>
          <span>{{ v.created_at }}</span>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.profile-workspace {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.workspace-header h1 {
  font-size: clamp(1.5rem, 3vw, 2rem);
  margin: 0;
}

.eyebrow {
  display: inline-block;
  padding: 0.3rem 0.65rem;
  border-radius: 999px;
  background: rgba(15, 118, 110, 0.1);
  color: #0f766e;
  font-size: 0.8rem;
  font-weight: 700;
  margin-bottom: 0.5rem;
}

.panel {
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid rgba(229, 231, 235, 0.9);
  border-radius: 24px;
  padding: 1.5rem;
  box-shadow: 0 18px 42px rgba(15, 23, 42, 0.08);
}

.section-head {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 1rem;
  flex-wrap: wrap;
}

.section-head h2 {
  margin: 0;
  flex: 1;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
  margin-bottom: 1rem;
}

.field label {
  font-size: 0.92rem;
  font-weight: 700;
}

.field input {
  width: 100%;
  border: 1px solid #dbe3ea;
  border-radius: 16px;
  padding: 0.85rem 1rem;
  background: #fbfcfd;
}

.primary-button,
.ghost-button {
  border: none;
  border-radius: 16px;
  padding: 0.6rem 1rem;
  cursor: pointer;
  transition: transform 0.15s ease, box-shadow 0.15s ease;
  font-size: 0.85rem;
}

.primary-button {
  background: linear-gradient(135deg, #0f766e, #14b8a6);
  color: white;
}

.ghost-button {
  background: #f8fafc;
  color: #0f172a;
  border: 1px solid #dbe3ea;
}

.primary-button:disabled,
.ghost-button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.banner {
  border-radius: 16px;
  padding: 0.8rem 1rem;
}

.success {
  background: #dcfce7;
  color: #166534;
}

.error {
  background: #fee2e2;
  color: #991b1b;
}

.display-box {
  background: #fafaf9;
  border: 1px solid #e5e7eb;
  border-radius: 16px;
  padding: 1rem;
  color: #6b7280;
}

.asset-list,
.version-list {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.asset-item,
.version-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  border-radius: 16px;
  padding: 1rem;
}

.asset-info,
.version-item {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.asset-info span,
.version-item span {
  color: #6b7280;
  font-size: 0.85rem;
}

.asset-actions {
  display: flex;
  gap: 0.5rem;
}

.error-text {
  color: #b91c1c;
}

.evidence-table {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.evidence-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.75rem;
  padding: 1rem;
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  border-radius: 16px;
}

.evidence-meta {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  flex: 1;
  min-width: 200px;
}

.confidence-badge {
  font-size: 0.8rem;
  color: #6b7280;
}

.diff-badge {
  font-size: 0.75rem;
  padding: 0.15rem 0.4rem;
  border-radius: 999px;
  background: #f1f5f9;
  border: 1px solid #dbe3ea;
}

.diff-badge.add { background: #dcfce7; color: #166534; }
.diff-badge.replace { background: #fef3c7; color: #92400e; }
.diff-badge.unchanged { background: #f1f5f9; color: #6b7280; }
.diff-badge.conflict { background: #fee2e2; color: #991b1b; }

.evidence-excerpt {
  flex: 2;
  min-width: 200px;
  color: #374151;
  font-size: 0.9rem;
}

.evidence-status {
  font-size: 0.85rem;
  color: #6b7280;
}

.evidence-decisions {
  display: flex;
  gap: 0.35rem;
}

.decision-btn {
  border: 1px solid #dbe3ea;
  border-radius: 12px;
  padding: 0.4rem 0.7rem;
  background: white;
  cursor: pointer;
  font-size: 0.8rem;
  transition: all 0.15s;
}

.decision-btn.active {
  background: #0f766e;
  color: white;
  border-color: #0f766e;
}

.decision-btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
