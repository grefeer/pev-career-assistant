<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useAuth } from "../../state/auth";
import { getSnapshot, getTaskEligibility, createTask } from "./snapshotApi";
import { listActiveDevices } from "../devices/deviceApi";
import type { SnapshotSummary, TaskEligibilityResponse } from "./snapshotTypes";
import type { DeviceSummary } from "../devices/deviceTypes";

const { token } = useAuth();
const route = useRoute();
const router = useRouter();

const snapshotId = route.params.id as string;

// ── State ───────────────────────────────────────────────────────────────────
const snapshot = ref<SnapshotSummary | null>(null);
const devices = ref<DeviceSummary[]>([]);
const eligibility = ref<TaskEligibilityResponse | null>(null);
const loading = ref(true);
const errorMessage = ref("");
const successMessage = ref("");

// Task creation
const selectedDeviceId = ref("");
const taskBusy = ref(false);
const taskCreated = ref(false);
const taskResult = ref<{ task_id: string; status: string } | null>(null);

// ── Computed ──────────────────────────────────────────────────────────────────
const expanded = ref(false);

const statusClass = computed(() => {
  if (!snapshot.value) return "";
  return `status-${snapshot.value.job_status_at_snapshot}`;
});

// ── Lifecycle ────────────────────────────────────────────────────────────────

onMounted(async () => {
  await loadDetail();
});

async function loadDetail() {
  if (!token.value) return;
  loading.value = true;
  errorMessage.value = "";
  try {
    const [snap, devResult] = await Promise.all([
      getSnapshot(token.value, snapshotId),
      listActiveDevices(token.value).catch(() => ({ devices: [] })),
    ]);
    snapshot.value = snap;
    devices.value = devResult.devices;

    // Fetch task eligibility
    try {
      eligibility.value = await getTaskEligibility(token.value, snapshotId);
    } catch {
      eligibility.value = null;
    }
  } catch (err: any) {
    errorMessage.value = err.message || "Failed to load snapshot.";
  } finally {
    loading.value = false;
  }
}

// ── Task creation ────────────────────────────────────────────────────────────

async function handleCreateTask() {
  if (!token.value || !snapshot.value) return;
  taskBusy.value = true;
  errorMessage.value = "";
  successMessage.value = "";
  try {
    const result = await createTask(
      token.value,
      snapshot.value.id,
      selectedDeviceId.value || undefined,
    );
    taskCreated.value = true;
    taskResult.value = { task_id: result.task_id, status: result.status };
    successMessage.value = `Task created successfully (status: ${result.status}).`;
  } catch (err: any) {
    errorMessage.value = err.message || "Failed to create task.";
  } finally {
    taskBusy.value = false;
  }
}

// ── Helpers ──────────────────────────────────────────────────────────────────

function formatDate(dateStr: string): string {
  try {
    const d = new Date(dateStr);
    return d.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return dateStr;
  }
}
</script>

<template>
  <div class="snapshot-detail">
    <!-- Loading -->
    <div v-if="loading" class="loading">
      <p>Loading snapshot...</p>
    </div>

    <!-- Error -->
    <div v-else-if="errorMessage && !snapshot" class="error-banner">
      <p>{{ errorMessage }}</p>
      <button class="btn btn-outline" @click="loadDetail">Retry</button>
    </div>

    <template v-else-if="snapshot">
      <!-- Header -->
      <div class="header">
        <span class="eyebrow">APPLICATION SNAPSHOT</span>
        <h2>{{ snapshot.company_name }} — {{ snapshot.title }}</h2>
        <div class="header-meta">
          <span :class="['badge', statusClass]">{{ snapshot.job_status_at_snapshot }}</span>
          <span v-if="snapshot.gui_eligible" class="badge badge-yes">GUI Eligible</span>
          <span v-else class="badge badge-no">No GUI</span>
          <span class="date">Created: {{ formatDate(snapshot.created_at) }}</span>
        </div>
      </div>

      <!-- Feedback messages -->
      <div v-if="successMessage" class="success-banner">
        <p>{{ successMessage }}</p>
      </div>
      <div v-if="errorMessage && snapshot" class="error-banner">
        <p>{{ errorMessage }}</p>
      </div>

      <!-- Content summary -->
      <div class="card content-summary">
        <div class="card-header" @click="expanded = !expanded">
          <h3>Content Summary</h3>
          <span class="toggle-icon">{{ expanded ? "▲" : "▼" }}</span>
        </div>

        <table class="info-table">
          <tr>
            <td class="label">Snapshot ID</td>
            <td><code>{{ snapshot.id }}</code></td>
          </tr>
          <tr>
            <td class="label">Job ID</td>
            <td><code>{{ snapshot.job_id }}</code></td>
          </tr>
          <tr>
            <td class="label">Approved Resume Version</td>
            <td><code>{{ snapshot.approved_resume_version_id }}</code></td>
          </tr>
          <tr>
            <td class="label">Profile Version</td>
            <td><code>{{ snapshot.profile_version_id }}</code></td>
          </tr>
          <tr>
            <td class="label">Schema Version</td>
            <td><code>{{ snapshot.schema_version }}</code></td>
          </tr>
          <tr>
            <td class="label">Job Version at Snapshot</td>
            <td>v{{ snapshot.job_review_version_at_snapshot }}</td>
          </tr>
        </table>

        <!-- Expandable non-sensitive fields placeholder -->
        <div v-if="expanded" class="expandable-section">
          <p class="expandable-note">
            Non-sensitive snapshot content is available. Dynamic answers and
            local-sensitive requirements are excluded for privacy.
          </p>
        </div>
      </div>

      <!-- Approved Resume Version info -->
      <div class="card">
        <h3>Approved Resume Version</h3>
        <table class="info-table">
          <tr>
            <td class="label">Version ID</td>
            <td><code>{{ snapshot.approved_resume_version_id }}</code></td>
          </tr>
        </table>
        <p class="hint">
          Resume attachments can be downloaded from the
          <router-link :to="`/resume-drafts/...`" class="disabled-link">draft review page</router-link>
          after approval.
        </p>
      </div>

      <!-- Task delivery section -->
      <div class="card">
        <h3>Delivery Task</h3>

        <!-- Eligibility status -->
        <div v-if="eligibility" class="eligibility-status">
          <span
            :class="['badge', eligibility.can_create_task ? 'badge-yes' : 'badge-no']"
          >
            {{ eligibility.can_create_task ? "Eligible" : "Not Eligible" }}
          </span>
          <span v-if="eligibility.reason_code" class="reason-code">
            {{ eligibility.reason_code }}
          </span>
        </div>
        <div v-else class="eligibility-status">
          <span class="badge badge-na">Unknown</span>
        </div>

        <!-- GUI eligible: device selector + create button -->
        <template v-if="snapshot.gui_eligible">
          <div class="form-field">
            <label for="device-select">Device (optional)</label>
            <select
              id="device-select"
              v-model="selectedDeviceId"
              :disabled="taskBusy || taskCreated"
            >
              <option value="">-- No device selected --</option>
              <option
                v-for="device in devices"
                :key="device.id"
                :value="device.id"
              >
                {{ device.name }}
                ({{ device.platform }}{{ device.online ? ", online" : ", offline" }})
              </option>
            </select>
            <p v-if="devices.length === 0" class="hint">
              No devices available. Please pair a device first.
            </p>
          </div>

          <div v-if="!taskCreated">
            <button
              class="btn btn-primary"
              :disabled="taskBusy"
              @click="handleCreateTask"
            >
              {{ taskBusy ? "Creating..." : "Create Delivery Task" }}
            </button>
          </div>

          <div v-else-if="taskResult" class="task-result">
            <h4>Task Created</h4>
            <table class="info-table">
              <tr>
                <td class="label">Task ID</td>
                <td><code>{{ taskResult.task_id }}</code></td>
              </tr>
              <tr>
                <td class="label">Status</td>
                <td><span class="badge badge-created">{{ taskResult.status }}</span></td>
              </tr>
            </table>
          </div>
        </template>

        <!-- Not GUI eligible: manual delivery notice -->
        <template v-else>
          <div class="notice-manual">
            <p>Manual delivery only.</p>
            <p class="hint">
              This job does not support GUI-based delivery. Please submit
              your application through the employer's standard application
              channel.
            </p>
          </div>
        </template>
      </div>

      <!-- Navigation -->
      <div class="nav-back">
        <button class="btn btn-outline" @click="router.push('/snapshots')">
          Back to Snapshots
        </button>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* ── Layout ─────────────────────────────────────────────────────────────────── */
.snapshot-detail {
  max-width: 800px;
  margin: 0 auto;
  padding: 1.5rem;
}

.loading {
  text-align: center;
  padding: 3rem 1rem;
  color: #6b7280;
}

.header {
  margin-bottom: 1.5rem;
}

.eyebrow {
  display: inline-block;
  padding: 0.3rem 0.65rem;
  border-radius: 999px;
  background: rgba(15, 118, 110, 0.1);
  color: #0f766e;
  font-size: 0.8rem;
  font-weight: 700;
  margin-bottom: 0.75rem;
}

h2 {
  font-size: clamp(1.3rem, 2.5vw, 1.75rem);
  margin: 0 0 0.5rem;
}

.header-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.75rem;
  font-size: 0.85rem;
  color: #6b7280;
}

.date {
  color: #9ca3af;
}

/* ── Cards ──────────────────────────────────────────────────────────────────── */
.card {
  padding: 1.25rem 1.5rem;
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid #e5e7eb;
  margin-bottom: 1rem;
}

.card h3 {
  margin: 0 0 0.75rem;
  font-size: 1.05rem;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  cursor: pointer;
  user-select: none;
}

.card-header h3 {
  margin: 0;
}

.toggle-icon {
  font-size: 0.8rem;
  color: #9ca3af;
}

/* ── Info table ─────────────────────────────────────────────────────────────── */
.info-table {
  width: 100%;
  border-collapse: collapse;
  margin-bottom: 0;
}

.info-table tr {
  border-bottom: 1px solid #f3f4f6;
}

.info-table tr:last-child {
  border-bottom: none;
}

.info-table td {
  padding: 0.5rem 0.25rem;
  font-size: 0.9rem;
  vertical-align: top;
}

.info-table td.label {
  width: 200px;
  color: #6b7280;
  font-weight: 600;
  font-size: 0.85rem;
  white-space: nowrap;
}

.info-table code {
  background: #f3f4f6;
  padding: 0.1rem 0.35rem;
  border-radius: 4px;
  font-size: 0.8rem;
  word-break: break-all;
}

/* ── Expandable section ─────────────────────────────────────────────────────── */
.expandable-section {
  margin-top: 0.75rem;
  padding-top: 0.75rem;
  border-top: 1px solid #e5e7eb;
}

.expandable-note {
  color: #6b7280;
  font-size: 0.85rem;
  font-style: italic;
}

/* ── Banners ────────────────────────────────────────────────────────────────── */
.success-banner,
.error-banner {
  padding: 0.75rem 1rem;
  border-radius: 12px;
  margin-bottom: 1rem;
  font-size: 0.9rem;
}

.success-banner {
  background: #d1fae5;
  border: 1px solid #a7f3d0;
  color: #065f46;
}

.error-banner {
  background: #fee2e2;
  border: 1px solid #fecaca;
  color: #991b1b;
}

/* ── Badges ─────────────────────────────────────────────────────────────────── */
.badge {
  display: inline-block;
  padding: 0.15rem 0.55rem;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 600;
}

.badge-yes { background: #d1fae5; color: #065f46; }
.badge-no { background: #f3f4f6; color: #6b7280; }
.badge-na { background: #fef3c7; color: #92400e; }
.badge-created { background: #dbeafe; color: #1e40af; }

.status-verified { background: #d1fae5; color: #065f46; }
.status-expired { background: #fee2e2; color: #991b1b; }
.status-rejected { background: #fee2e2; color: #991b1b; }
.status-pending_review { background: #fef3c7; color: #92400e; }
.status-pending_completion { background: #fef3c7; color: #92400e; }

/* ── Eligibility ────────────────────────────────────────────────────────────── */
.eligibility-status {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 1rem;
}

.reason-code {
  font-size: 0.85rem;
  color: #6b7280;
}

/* ── Form ───────────────────────────────────────────────────────────────────── */
.form-field {
  margin-bottom: 1rem;
}

.form-field label {
  display: block;
  font-size: 0.85rem;
  font-weight: 600;
  color: #374151;
  margin-bottom: 0.3rem;
}

.form-field select {
  width: 100%;
  padding: 0.6rem 0.8rem;
  border: 1px solid #d1d5db;
  border-radius: 10px;
  font-size: 0.9rem;
  background: #fff;
  color: #1f2937;
  box-sizing: border-box;
  font-family: inherit;
  cursor: pointer;
}

.form-field select:focus {
  outline: none;
  border-color: #3b82f6;
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
}

.hint {
  font-size: 0.8rem;
  color: #9ca3af;
  margin-top: 0.3rem;
}

/* ── Buttons ────────────────────────────────────────────────────────────────── */
.btn {
  display: inline-flex;
  align-items: center;
  padding: 0.6rem 1.3rem;
  border: none;
  border-radius: 10px;
  font-weight: 600;
  font-size: 0.9rem;
  cursor: pointer;
  transition: background 0.15s;
  text-decoration: none;
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-primary {
  background: #059669;
  color: #fff;
}

.btn-primary:hover:not(:disabled) {
  background: #047857;
}

.btn-outline {
  background: transparent;
  border: 1px solid #d1d5db;
  color: #374151;
}

.btn-outline:hover {
  background: #f3f4f6;
}

/* ── Manual delivery notice ─────────────────────────────────────────────────── */
.notice-manual {
  padding: 1rem;
  border-radius: 12px;
  background: #fffbeb;
  border: 1px solid #fde68a;
}

.notice-manual p {
  margin: 0 0 0.3rem;
  font-weight: 600;
  color: #92400e;
}

/* ── Task result ────────────────────────────────────────────────────────────── */
.task-result {
  margin-top: 1rem;
  padding: 1rem;
  border-radius: 12px;
  background: #f0fdf4;
  border: 1px solid #a7f3d0;
}

.task-result h4 {
  margin: 0 0 0.5rem;
  font-size: 0.95rem;
  color: #065f46;
}

/* ── Disabled link ──────────────────────────────────────────────────────────── */
.disabled-link {
  color: #9ca3af;
  cursor: not-allowed;
  text-decoration: none;
}

.disabled-link:hover {
  text-decoration: underline;
}

/* ── Navigation ─────────────────────────────────────────────────────────────── */
.nav-back {
  margin-top: 1.5rem;
}
</style>
