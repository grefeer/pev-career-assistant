<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useAuth } from "../../state/auth";
import { ApiError } from "../../api";
import { getMatch } from "./matchingApi";
import {
  approveDraft,
  downloadAttachment,
  getDraft,
  rejectDraft,
} from "./draftApi";
import * as snapshotApi from "../snapshots/snapshotApi";
import type {
  AttachmentInfo,
  ApprovedResumeVersionResponse,
  ResumeDraftResponse,
} from "./draftTypes";
import type { MatchReportResponse } from "./matchingTypes";

const { token } = useAuth();
const route = useRoute();
const router = useRouter();

const draftId = route.params.draftId as string;

// ── State ───────────────────────────────────────────────────────────────────
const draft = ref<ResumeDraftResponse | null>(null);
const matchReport = ref<MatchReportResponse | null>(null);
const approvedVersion = ref<ApprovedResumeVersionResponse | null>(null);
const loading = ref(true);
const actionBusy = ref(false);
const errorMessage = ref("");
const successMessage = ref("");

// Snapshot creation form
const dynamicAnswersText = ref("[]");
const localSensitiveRefsText = ref("[]");
const snapshotCreating = ref(false);
const snapshotError = ref("");
const snapshotCreated = ref(false);

// ── Lifecycle ────────────────────────────────────────────────────────────────

onMounted(async () => {
  await loadDraft();
});

async function loadDraft() {
  if (!token.value) return;
  loading.value = true;
  errorMessage.value = "";
  try {
    const d = await getDraft(token.value, draftId);
    draft.value = d;

    // Fetch match report to get job_id for snapshot creation
    if (d.match_report_id) {
      try {
        matchReport.value = await getMatch(token.value, d.match_report_id);
      } catch {
        // Non-critical; match fetch failure should not block the diff view
        matchReport.value = null;
      }
    }
  } catch (err: any) {
    errorMessage.value = err.message || "Failed to load draft";
  } finally {
    loading.value = false;
  }
}

// ── Diff rendering helpers ───────────────────────────────────────────────────

function diffLabel(op: string): string {
  const labels: Record<string, string> = {
    highlight: "New / Highlighted",
    rephrase: "Rephrased",
    reorder: "Reordered",
    omit: "Omitted",
    summarize: "Summarized",
  };
  return labels[op] || op;
}

// ── Actions ──────────────────────────────────────────────────────────────────

async function handleApprove() {
  if (!token.value || !draft.value) return;
  actionBusy.value = true;
  errorMessage.value = "";
  successMessage.value = "";
  try {
    const result = await approveDraft(
      token.value,
      draft.value.id,
      draft.value.state_version,
    );
    approvedVersion.value = result;
    // Reload draft to reflect approved status
    await loadDraft();
    successMessage.value = "Draft approved successfully.";
  } catch (err: any) {
    if (err instanceof ApiError && err.status === 409) {
      // Stale version — reload draft and retry
      await loadDraft();
      errorMessage.value =
        "Version conflict detected. The draft was reloaded. Please try again.";
    } else {
      errorMessage.value = err.message || "Failed to approve draft.";
    }
  } finally {
    actionBusy.value = false;
  }
}

async function handleReject() {
  if (!token.value || !draft.value) return;
  actionBusy.value = true;
  errorMessage.value = "";
  successMessage.value = "";
  try {
    await rejectDraft(token.value, draft.value.id, draft.value.state_version);
    await loadDraft();
    successMessage.value = "Draft rejected.";
  } catch (err: any) {
    if (err instanceof ApiError && err.status === 409) {
      await loadDraft();
      errorMessage.value =
        "Version conflict detected. The draft was reloaded. Please try again.";
    } else {
      errorMessage.value = err.message || "Failed to reject draft.";
    }
  } finally {
    actionBusy.value = false;
  }
}

async function handleDownload(att: AttachmentInfo) {
  if (!token.value) return;
  const ext = att.format === "pdf" ? "pdf" : "docx";
  const filename = `resume-${draftId}.${ext}`;
  try {
    await downloadAttachment(token.value, att.id, filename);
  } catch (err: any) {
    errorMessage.value = err.message || "Download failed.";
  }
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ── Snapshot creation ────────────────────────────────────────────────────────

async function handleCreateSnapshot() {
  if (!token.value || !approvedVersion.value || !matchReport.value) return;
  snapshotError.value = "";

  let dynamicAnswers: Record<string, unknown>[];
  let localSensitiveReqs: Record<string, unknown>[];

  try {
    dynamicAnswers = JSON.parse(dynamicAnswersText.value);
    if (!Array.isArray(dynamicAnswers)) throw new Error("not an array");
  } catch {
    snapshotError.value = "Dynamic answers must be a valid JSON array.";
    return;
  }

  try {
    localSensitiveReqs = JSON.parse(localSensitiveRefsText.value);
    if (!Array.isArray(localSensitiveReqs)) throw new Error("not an array");
  } catch {
    snapshotError.value = "Local sensitive requirements must be a valid JSON array.";
    return;
  }

  snapshotCreating.value = true;
  try {
    await snapshotApi.createSnapshot(token.value, {
      job_id: matchReport.value.job_id,
      approved_resume_version_id: approvedVersion.value.id,
      dynamic_answers: dynamicAnswers,
      local_sensitive_requirements: localSensitiveReqs,
    });
    snapshotCreated.value = true;
    successMessage.value = "Application snapshot created successfully.";
  } catch (err: any) {
    snapshotError.value = err.message || "Failed to create snapshot.";
  } finally {
    snapshotCreating.value = false;
  }
}
</script>

<template>
  <div class="draft-review">
    <!-- Loading state -->
    <div v-if="loading" class="loading">
      <p>Loading draft...</p>
    </div>

    <!-- Error state -->
    <div v-else-if="errorMessage && !draft" class="error-banner">
      <p>{{ errorMessage }}</p>
      <button @click="loadDraft">Retry</button>
    </div>

    <template v-else-if="draft">
      <!-- Header -->
      <div class="header">
        <span class="eyebrow">RESUME DRAFT REVIEW</span>
        <h2>{{ draft.company_name }} — {{ draft.job_title }}</h2>
        <span :class="['status-badge', `status-${draft.status}`]">
          {{ draft.status }}
        </span>
      </div>

      <!-- Success message -->
      <div v-if="successMessage" class="success-banner">
        <p>{{ successMessage }}</p>
      </div>

      <!-- Error message -->
      <div v-if="errorMessage && draft" class="error-banner">
        <p>{{ errorMessage }}</p>
      </div>

      <!-- Diff view (status === 'draft') -->
      <div v-if="draft.status === 'draft'" class="diff-section">
        <div
          v-for="diff in (draft.diffs ?? [])"
          :key="diff.fact_ref"
          :class="['diff-card', `diff-${diff.op}`]"
        >
          <div class="diff-header">
            <span class="diff-section-label">{{ diff.section }}</span>
            <span :class="['op-badge', `op-${diff.op}`]">{{ diffLabel(diff.op) }}</span>
          </div>

          <div class="diff-body">
            <div v-if="diff.before" class="diff-panel before-panel">
              <p class="panel-label">Original</p>
              <p class="panel-text">{{ diff.before }}</p>
            </div>
            <div v-if="diff.after" class="diff-panel after-panel">
              <p class="panel-label">Modified</p>
              <p class="panel-text">{{ diff.after }}</p>
            </div>
          </div>

          <div class="diff-meta">
            <span class="meta-fact">Fact ref: <code>{{ diff.fact_ref }}</code></span>
            <span class="meta-evidence">Evidence: <code>{{ diff.evidence_ids.join(", ") }}</code></span>
          </div>
        </div>

        <!-- Action buttons -->
        <div class="actions">
          <button
            class="btn btn-approve"
            :disabled="actionBusy"
            @click="handleApprove"
          >
            {{ actionBusy ? "Processing..." : "Approve" }}
          </button>
          <button
            class="btn btn-reject"
            :disabled="actionBusy"
            @click="handleReject"
          >
            {{ actionBusy ? "Processing..." : "Reject" }}
          </button>
        </div>
      </div>

      <!-- Approved state -->
      <div v-else-if="draft.status === 'approved'" class="approved-section">
        <div class="approved-card">
          <h3>Approved Resume Version</h3>
          <p v-if="approvedVersion">
            Approved at: <strong>{{ approvedVersion.approved_at }}</strong>
          </p>
          <p v-if="approvedVersion">
            Version ID: <code>{{ approvedVersion.id }}</code>
          </p>

          <h4>Attachments</h4>
          <div v-if="approvedVersion?.attachments?.length" class="attachment-list">
            <button
              v-for="att in approvedVersion.attachments"
              :key="att.id"
              class="btn btn-download"
              @click="handleDownload(att)"
            >
              Download {{ att.format.toUpperCase() }}
              <span class="size">({{ formatSize(att.plaintext_size) }})</span>
            </button>
          </div>
          <p v-else class="no-data">No attachments available.</p>
        </div>

        <!-- Create Application Snapshot form -->
        <div class="snapshot-form-card">
          <h3>Create Application Snapshot</h3>
          <p class="form-hint">
            Create a frozen snapshot of this approved resume for a job application.
          </p>

          <div v-if="snapshotCreated" class="success-banner">
            <p>Snapshot created! You can now view it in the
              <router-link to="/snapshots">Snapshots</router-link> page.
            </p>
          </div>

          <template v-if="!snapshotCreated">
            <div class="form-field">
              <label>Job ID (from match report)</label>
              <input
                :value="matchReport?.job_id || '—'"
                readonly
                class="readonly-input"
              />
            </div>

            <div class="form-field">
              <label>Approved Resume Version ID</label>
              <input
                :value="approvedVersion?.id || '—'"
                readonly
                class="readonly-input"
              />
            </div>

            <div class="form-field">
              <label>Dynamic Answers (JSON array)</label>
              <textarea
                v-model="dynamicAnswersText"
                rows="4"
                placeholder='[{"field": "value"}]'
              ></textarea>
            </div>

            <div class="form-field">
              <label>Local Sensitive Requirements (JSON array)</label>
              <textarea
                v-model="localSensitiveRefsText"
                rows="4"
                placeholder='[{"category": "id_card", "reference": "hash"}]'
              ></textarea>
            </div>

            <div v-if="snapshotError" class="error-banner">
              <p>{{ snapshotError }}</p>
            </div>

            <button
              class="btn btn-primary"
              :disabled="snapshotCreating"
              @click="handleCreateSnapshot"
            >
              {{ snapshotCreating ? "Creating..." : "Create Snapshot" }}
            </button>
          </template>
        </div>
      </div>

      <!-- Rejected state -->
      <div v-else-if="draft.status === 'rejected'" class="rejected-section">
        <div class="notice-card notice-rejected">
          <h3>Draft Rejected</h3>
          <p>This resume draft has been rejected.</p>
        </div>
      </div>

      <!-- Other status (e.g. generating) -->
      <div v-else class="pending-section">
        <div class="notice-card">
          <h3>Draft Status: {{ draft.status }}</h3>
          <p>
            This draft is currently <strong>{{ draft.status }}</strong>.
            Please wait for it to become available.
          </p>
          <p v-if="draft.error_code" class="error-code">
            Error: {{ draft.error_code }}
          </p>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
/* ── Layout ─────────────────────────────────────────────────────────────────── */
.draft-review {
  max-width: 1000px;
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

.status-badge {
  display: inline-block;
  padding: 0.2rem 0.6rem;
  border-radius: 999px;
  font-size: 0.8rem;
  font-weight: 600;
  background: #e5e7eb;
  color: #374151;
}

.status-draft { background: #dbeafe; color: #1e40af; }
.status-approved { background: #d1fae5; color: #065f46; }
.status-rejected { background: #fee2e2; color: #991b1b; }
.status-generating { background: #fef3c7; color: #92400e; }

/* ── Banners ────────────────────────────────────────────────────────────────── */
.success-banner {
  padding: 0.75rem 1rem;
  border-radius: 12px;
  background: #d1fae5;
  border: 1px solid #a7f3d0;
  color: #065f46;
  margin-bottom: 1rem;
  font-size: 0.9rem;
}

.success-banner a {
  color: #065f46;
  font-weight: 600;
}

.error-banner {
  padding: 0.75rem 1rem;
  border-radius: 12px;
  background: #fee2e2;
  border: 1px solid #fecaca;
  color: #991b1b;
  margin-bottom: 1rem;
  font-size: 0.9rem;
}

/* ── Diff cards ─────────────────────────────────────────────────────────────── */
.diff-section {
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.diff-card {
  border: 1px solid #e5e7eb;
  border-radius: 16px;
  padding: 1.2rem;
  background: rgba(255, 255, 255, 0.92);
  transition: box-shadow 0.15s;
}

.diff-card:hover {
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
}

/* Color coding by op */
.diff-highlight {
  border-left: 4px solid #10b981;
  background: #f0fdf4;
}
.diff-rephrase {
  border-left: 4px solid #f59e0b;
  background: #fffbeb;
}
.diff-reorder {
  border-left: 4px solid #3b82f6;
  background: #eff6ff;
}
.diff-omit {
  border-left: 4px solid #ef4444;
  background: #fef2f2;
}
.diff-summarize {
  border-left: 4px solid #f59e0b;
  background: #fffbeb;
}

.diff-header {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  margin-bottom: 0.75rem;
}

.diff-section-label {
  font-weight: 700;
  font-size: 0.9rem;
  color: #374151;
}

.op-badge {
  display: inline-block;
  padding: 0.15rem 0.5rem;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 600;
}

.op-highlight { background: #a7f3d0; color: #065f46; }
.op-rephrase { background: #fde68a; color: #92400e; }
.op-reorder { background: #bfdbfe; color: #1e40af; }
.op-omit { background: #fecaca; color: #991b1b; }
.op-summarize { background: #fde68a; color: #92400e; }

.diff-body {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
  margin-bottom: 0.75rem;
}

.diff-panel {
  padding: 0.75rem 1rem;
  border-radius: 10px;
  border: 1px solid #e5e7eb;
  background: #fff;
}

.panel-label {
  font-size: 0.7rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #9ca3af;
  margin: 0 0 0.4rem;
}

.panel-text {
  margin: 0;
  font-size: 0.9rem;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: #1f2937;
}

.diff-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 1rem;
  font-size: 0.8rem;
  color: #6b7280;
}

.meta-fact code,
.meta-evidence code {
  background: #f3f4f6;
  padding: 0.1rem 0.35rem;
  border-radius: 4px;
  font-size: 0.75rem;
  color: #4b5563;
}

/* ── Buttons ────────────────────────────────────────────────────────────────── */
.actions {
  display: flex;
  gap: 1rem;
  margin-top: 1.5rem;
}

.btn {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.65rem 1.4rem;
  border: none;
  border-radius: 12px;
  font-weight: 600;
  font-size: 0.9rem;
  cursor: pointer;
  transition: background 0.15s, opacity 0.15s;
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-approve {
  background: #059669;
  color: #fff;
}

.btn-approve:hover:not(:disabled) {
  background: #047857;
}

.btn-reject {
  background: #dc2626;
  color: #fff;
}

.btn-reject:hover:not(:disabled) {
  background: #b91c1c;
}

.btn-download {
  background: #0f766e;
  color: #fff;
  padding: 0.5rem 1rem;
  font-size: 0.85rem;
}

.btn-download:hover:not(:disabled) {
  background: #0d6b63;
}

.btn-primary {
  background: #2563eb;
  color: #fff;
}

.btn-primary:hover:not(:disabled) {
  background: #1d4ed8;
}

.size {
  font-weight: 400;
  opacity: 0.8;
  font-size: 0.8rem;
}

/* ── Approved section ───────────────────────────────────────────────────────── */
.approved-section {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

.approved-card,
.snapshot-form-card {
  padding: 1.5rem;
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid #e5e7eb;
}

.approved-card h3,
.snapshot-form-card h3 {
  margin: 0 0 1rem;
  font-size: 1.1rem;
}

.approved-card h4 {
  margin: 1rem 0 0.5rem;
  font-size: 0.95rem;
  color: #374151;
}

.approved-card code {
  background: #f3f4f6;
  padding: 0.1rem 0.35rem;
  border-radius: 4px;
  font-size: 0.8rem;
}

.attachment-list {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
}

.no-data {
  color: #9ca3af;
  font-style: italic;
  font-size: 0.9rem;
}

/* ── Snapshot form ──────────────────────────────────────────────────────────── */
.form-hint {
  color: #6b7280;
  font-size: 0.9rem;
  margin-bottom: 1rem;
}

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

.form-field input,
.form-field textarea {
  width: 100%;
  padding: 0.6rem 0.8rem;
  border: 1px solid #d1d5db;
  border-radius: 10px;
  font-size: 0.9rem;
  background: #fff;
  color: #1f2937;
  box-sizing: border-box;
  font-family: inherit;
}

.form-field textarea {
  resize: vertical;
  min-height: 80px;
}

.form-field input:focus,
.form-field textarea:focus {
  outline: none;
  border-color: #3b82f6;
  box-shadow: 0 0 0 3px rgba(59, 130, 246, 0.15);
}

.readonly-input {
  background: #f9fafb !important;
  color: #6b7280 !important;
  cursor: not-allowed;
}

/* ── Rejected / pending notice ──────────────────────────────────────────────── */
.rejected-section,
.pending-section {
  margin-top: 1rem;
}

.notice-card {
  padding: 1.5rem;
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid #e5e7eb;
}

.notice-card h3 {
  margin: 0 0 0.5rem;
}

.notice-rejected {
  border-left: 4px solid #ef4444;
}

.error-code {
  color: #dc2626;
  font-size: 0.85rem;
  margin-top: 0.5rem;
}
</style>
