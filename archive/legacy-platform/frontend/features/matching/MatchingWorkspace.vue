<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth } from '../../state/auth'
import { fetchVerifiedJobs } from '../jobs/jobsApi'
import { fetchProfileVersions } from '../profile/profileApi'
import { createMatch, getMatch, generateResumeDraft } from './matchingApi'
import type { JobOption, ProfileVersionOption, MatchReportResponse, RequirementAssessment } from './matchingTypes'

const router = useRouter()
const { token } = useAuth()

const jobs = ref<JobOption[]>([])
const profileVersions = ref<ProfileVersionOption[]>([])
const selectedJobId = ref<string>('')
const selectedProfileVersionId = ref<string>('')
const loadingJobs = ref(false)
const loadingVersions = ref(false)

const match = ref<MatchReportResponse | null>(null)
const matchLoading = ref(false)
const matchError = ref<string | null>(null)
const generatingResume = ref(false)

let pollTimer: ReturnType<typeof setInterval> | null = null

// Expandable card state
const expandedSections = ref<Record<string, boolean>>({
  strengths: false,
  gaps: false,
  unknowns: false,
  risks: false,
})

function toggleSection(section: string) {
  expandedSections.value[section] = !expandedSections.value[section]
}

onMounted(async () => {
  await Promise.all([loadJobs(), loadProfileVersions()])
})

onUnmounted(() => {
  stopPolling()
})

function stopPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

async function loadJobs() {
  if (!token.value) return
  loadingJobs.value = true
  try {
    const resp = await fetchVerifiedJobs(token.value, { limit: 100, offset: 0 })
    jobs.value = resp.jobs.map((j) => ({
      id: j.id,
      title: j.title,
      company_name: j.company_name,
    }))
  } catch (err: unknown) {
    console.error('Failed to load jobs', err)
  } finally {
    loadingJobs.value = false
  }
}

async function loadProfileVersions() {
  if (!token.value) return
  loadingVersions.value = true
  try {
    const resp = await fetchProfileVersions(token.value)
    profileVersions.value = (resp.versions || []).map((v) => ({
      id: v.id,
      version_number: v.version_number,
      created_at: v.created_at,
    }))
  } catch (err: unknown) {
    console.error('Failed to load profile versions', err)
  } finally {
    loadingVersions.value = false
  }
}

async function startMatch() {
  if (!token.value || !selectedJobId.value || !selectedProfileVersionId.value) return

  stopPolling()
  matchLoading.value = true
  matchError.value = null
  match.value = null

  try {
    const created = await createMatch(
      token.value,
      selectedJobId.value,
      selectedProfileVersionId.value,
    )
    // If the match returns completed immediately, show results
    if (created.status === 'completed' || created.status === 'failed') {
      match.value = created
      matchLoading.value = false
      return
    }
    // Otherwise poll until completion
    match.value = created
    startPolling(created.id)
  } catch (err: unknown) {
    matchLoading.value = false
    if (err && typeof err === 'object' && 'message' in err) {
      matchError.value = (err as { message: string }).message
    } else {
      matchError.value = 'Failed to start match'
    }
  }
}

function startPolling(matchId: string) {
  pollTimer = setInterval(async () => {
    if (!token.value) return
    try {
      const updated = await getMatch(token.value, matchId)
      match.value = updated
      if (updated.status === 'completed' || updated.status === 'failed') {
        stopPolling()
        matchLoading.value = false
      }
    } catch (err: unknown) {
      stopPolling()
      matchLoading.value = false
      if (err && typeof err === 'object' && 'message' in err) {
        matchError.value = (err as { message: string }).message
      } else {
        matchError.value = 'Failed to poll match status'
      }
    }
  }, 2000)
}

async function handleGenerateResume() {
  if (!token.value || !match.value) return
  generatingResume.value = true
  try {
    const draft = await generateResumeDraft(token.value, match.value.id)
    router.push(`/resume-drafts/${draft.id}`)
  } catch (err: unknown) {
    if (err && typeof err === 'object' && 'message' in err) {
      matchError.value = (err as { message: string }).message
    } else {
      matchError.value = 'Failed to generate resume draft'
    }
  } finally {
    generatingResume.value = false
  }
}

function scorePercentage(): number {
  if (!match.value || match.value.score === null) return 0
  return Math.round(match.value.score * 100)
}

function scoreColor(): string {
  const pct = scorePercentage()
  if (pct >= 80) return '#16a34a'
  if (pct >= 60) return '#ca8a04'
  if (pct >= 40) return '#ea580c'
  return '#dc2626'
}

function priorityClass(): string {
  if (!match.value?.application_priority) return ''
  const p = match.value.application_priority.toLowerCase()
  if (p === 'high') return 'priority-high'
  if (p === 'medium') return 'priority-medium'
  return 'priority-low'
}

function getAssessments(type: 'strengths' | 'gaps' | 'unknowns' | 'risks'): RequirementAssessment[] {
  if (!match.value) return []
  const data = match.value[type]
  if (!data) return []
  return data
}
</script>

<template>
  <div class="matching-workspace">
    <div class="workspace-header">
      <span class="eyebrow">MATCHING WORKSPACE</span>
      <h2>岗位匹配评估</h2>
    </div>

    <!-- Selection Panel -->
    <div class="selection-panel">
      <div class="form-row">
        <label for="job-select">已核验岗位</label>
        <select
          id="job-select"
          v-model="selectedJobId"
          :disabled="loadingJobs || matchLoading"
        >
          <option value="" disabled>
            {{ loadingJobs ? '加载中...' : '请选择岗位' }}
          </option>
          <option
            v-for="job in jobs"
            :key="job.id"
            :value="job.id"
          >
            {{ job.title }} @ {{ job.company_name }}
          </option>
        </select>
      </div>

      <div class="form-row">
        <label for="profile-version-select">已确认简历版本</label>
        <select
          id="profile-version-select"
          v-model="selectedProfileVersionId"
          :disabled="loadingVersions || matchLoading"
        >
          <option value="" disabled>
            {{ loadingVersions ? '加载中...' : '请选择版本' }}
          </option>
          <option
            v-for="pv in profileVersions"
            :key="pv.id"
            :value="pv.id"
          >
            v{{ pv.version_number }} ({{ new Date(pv.created_at).toLocaleDateString() }})
          </option>
        </select>
      </div>

      <button
        class="btn btn-primary"
        :disabled="!selectedJobId || !selectedProfileVersionId || matchLoading"
        @click="startMatch"
      >
        {{ matchLoading ? '匹配进行中...' : '开始匹配' }}
      </button>
    </div>

    <!-- Error Display -->
    <div v-if="matchError" class="error-banner">
      {{ matchError }}
    </div>

    <!-- Match Running Indicator -->
    <div v-if="matchLoading && match && match.status === 'running'" class="running-indicator">
      <div class="spinner"></div>
      <p>正在分析匹配...</p>
    </div>

    <!-- Match Results -->
    <div v-if="match && match.status === 'completed'" class="match-results">
      <!-- Score Bar -->
      <div class="score-section">
        <h3>匹配分数</h3>
        <div class="score-bar-container">
          <div class="score-bar-bg">
            <div
              class="score-bar-fill"
              :style="{ width: scorePercentage() + '%', backgroundColor: scoreColor() }"
            ></div>
          </div>
          <span class="score-label" :style="{ color: scoreColor() }">
            {{ scorePercentage() }}%
          </span>
        </div>
      </div>

      <!-- Priority Badge -->
      <div v-if="match.application_priority" class="priority-section">
        <h3>申请优先级</h3>
        <span class="priority-badge" :class="priorityClass()">
          {{ match.application_priority }}
        </span>
      </div>

      <!-- Score Components -->
      <div v-if="match.score_components && match.score_components.length > 0" class="components-section">
        <h3>分数构成</h3>
        <div class="components-list">
          <div
            v-for="comp in match.score_components"
            :key="comp.requirement_id"
            class="component-item"
          >
            <span class="component-id">{{ comp.requirement_id }}</span>
            <span class="component-score">
              {{ comp.earned_basis_points }} / {{ comp.weight_basis_points }}
            </span>
          </div>
        </div>
      </div>

      <!-- Recommendation -->
      <div v-if="match.recommendation" class="recommendation-section">
        <h3>建议</h3>
        <p class="recommendation-text">{{ match.recommendation.text }}</p>
      </div>

      <!-- Expandable Assessment Sections -->
      <div class="assessments">
        <!-- Strengths -->
        <div v-if="getAssessments('strengths').length > 0" class="assessment-card strengths-card">
          <button class="card-header" @click="toggleSection('strengths')">
            <span class="card-title">优势 ({{ getAssessments('strengths').length }})</span>
            <span class="toggle-icon">{{ expandedSections.strengths ? '▼' : '▶' }}</span>
          </button>
          <div v-if="expandedSections.strengths" class="card-body">
            <div
              v-for="item in getAssessments('strengths')"
              :key="item.requirement_id"
              class="assessment-item"
            >
              <div class="item-header">
                <span class="verdict satisfied">已满足</span>
                <span class="requirement-text">{{ item.requirement }}</span>
              </div>
              <p class="item-detail">{{ item.detail }}</p>
              <div class="item-evidence">
                <span class="evidence-label">证据:</span>
                <span class="evidence-ids">{{ item.evidence_ids.join(', ') || '无' }}</span>
              </div>
            </div>
          </div>
        </div>

        <!-- Gaps -->
        <div v-if="getAssessments('gaps').length > 0" class="assessment-card gaps-card">
          <button class="card-header" @click="toggleSection('gaps')">
            <span class="card-title">差距 ({{ getAssessments('gaps').length }})</span>
            <span class="toggle-icon">{{ expandedSections.gaps ? '▼' : '▶' }}</span>
          </button>
          <div v-if="expandedSections.gaps" class="card-body">
            <div
              v-for="item in getAssessments('gaps')"
              :key="item.requirement_id"
              class="assessment-item"
            >
              <div class="item-header">
                <span class="verdict gap">差距</span>
                <span class="requirement-text">{{ item.requirement }}</span>
              </div>
              <p class="item-detail">{{ item.detail }}</p>
              <div class="item-evidence">
                <span class="evidence-label">证据:</span>
                <span class="evidence-ids">{{ item.evidence_ids.join(', ') || '无' }}</span>
              </div>
            </div>
          </div>
        </div>

        <!-- Unknowns -->
        <div v-if="getAssessments('unknowns').length > 0" class="assessment-card unknowns-card">
          <button class="card-header" @click="toggleSection('unknowns')">
            <span class="card-title">未知 ({{ getAssessments('unknowns').length }})</span>
            <span class="toggle-icon">{{ expandedSections.unknowns ? '▼' : '▶' }}</span>
          </button>
          <div v-if="expandedSections.unknowns" class="card-body">
            <div
              v-for="item in getAssessments('unknowns')"
              :key="item.requirement_id"
              class="assessment-item"
            >
              <div class="item-header">
                <span class="verdict unknown">未知</span>
                <span class="requirement-text">{{ item.requirement }}</span>
              </div>
              <p class="item-detail">{{ item.detail }}</p>
              <div class="item-evidence">
                <span class="evidence-label">证据:</span>
                <span class="evidence-ids">{{ item.evidence_ids.join(', ') || '无' }}</span>
              </div>
            </div>
          </div>
        </div>

        <!-- Risks -->
        <div v-if="getAssessments('risks').length > 0" class="assessment-card risks-card">
          <button class="card-header" @click="toggleSection('risks')">
            <span class="card-title">风险 ({{ getAssessments('risks').length }})</span>
            <span class="toggle-icon">{{ expandedSections.risks ? '▼' : '▶' }}</span>
          </button>
          <div v-if="expandedSections.risks" class="card-body">
            <div
              v-for="item in getAssessments('risks')"
              :key="item.requirement_id"
              class="assessment-item"
            >
              <div class="item-header">
                <span class="verdict risk">风险</span>
                <span class="requirement-text">{{ item.requirement }}</span>
              </div>
              <p class="item-detail">{{ item.detail }}</p>
              <div class="item-evidence">
                <span class="evidence-label">证据:</span>
                <span class="evidence-ids">{{ item.evidence_ids.join(', ') || '无' }}</span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- Generate Custom Resume Button -->
      <div class="actions">
        <button
          class="btn btn-secondary"
          :disabled="generatingResume"
          @click="handleGenerateResume"
        >
          {{ generatingResume ? '生成中...' : '生成定制简历' }}
        </button>
      </div>
    </div>

    <!-- Match Failed -->
    <div v-if="match && match.status === 'failed'" class="match-failed">
      <h3>匹配失败</h3>
      <p v-if="match.error_code">错误代码: {{ match.error_code }}</p>
    </div>
  </div>
</template>

<style scoped>
.matching-workspace {
  max-width: 800px;
  margin: 0 auto;
  padding: 2rem;
}

.workspace-header {
  margin-bottom: 2rem;
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

h2 {
  font-size: clamp(1.5rem, 3vw, 2rem);
  margin: 0;
}

h3 {
  font-size: 1.1rem;
  margin: 0 0 0.75rem;
  color: #1f2937;
}

/* Selection Panel */
.selection-panel {
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid #e5e7eb;
  border-radius: 24px;
  padding: 1.5rem;
  margin-bottom: 1.5rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.form-row {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.form-row label {
  font-weight: 600;
  font-size: 0.9rem;
  color: #374151;
}

.form-row select {
  padding: 0.65rem 0.75rem;
  border: 1px solid #d1d5db;
  border-radius: 12px;
  background: #fff;
  font-size: 0.95rem;
  color: #111827;
  transition: border-color 0.15s;
}

.form-row select:focus {
  outline: none;
  border-color: #0f766e;
  box-shadow: 0 0 0 3px rgba(15, 118, 110, 0.15);
}

.form-row select:disabled {
  background: #f3f4f6;
  cursor: not-allowed;
}

/* Buttons */
.btn {
  padding: 0.7rem 1.5rem;
  border: none;
  border-radius: 12px;
  font-size: 0.95rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s, opacity 0.15s;
}

.btn:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.btn-primary {
  background: #0f766e;
  color: #fff;
}

.btn-primary:hover:not(:disabled) {
  background: #115e59;
}

.btn-secondary {
  background: #4f46e5;
  color: #fff;
}

.btn-secondary:hover:not(:disabled) {
  background: #4338ca;
}

/* Error Banner */
.error-banner {
  background: #fef2f2;
  color: #b91c1c;
  border: 1px solid #fecaca;
  border-radius: 12px;
  padding: 0.75rem 1rem;
  margin-bottom: 1rem;
  font-size: 0.9rem;
}

/* Running Indicator */
.running-indicator {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  padding: 1rem;
  background: rgba(15, 118, 110, 0.06);
  border: 1px solid #e5e7eb;
  border-radius: 16px;
  margin-bottom: 1rem;
}

.spinner {
  width: 20px;
  height: 20px;
  border: 3px solid #e5e7eb;
  border-top-color: #0f766e;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
}

@keyframes spin {
  to { transform: rotate(360deg); }
}

/* Match Results */
.match-results {
  display: flex;
  flex-direction: column;
  gap: 1.5rem;
}

/* Score Bar */
.score-section {
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid #e5e7eb;
  border-radius: 24px;
  padding: 1.5rem;
}

.score-bar-container {
  display: flex;
  align-items: center;
  gap: 1rem;
}

.score-bar-bg {
  flex: 1;
  height: 24px;
  background: #e5e7eb;
  border-radius: 12px;
  overflow: hidden;
}

.score-bar-fill {
  height: 100%;
  border-radius: 12px;
  transition: width 0.5s ease;
  min-width: 4px;
}

.score-label {
  font-size: 1.25rem;
  font-weight: 700;
  min-width: 3.5rem;
  text-align: right;
}

/* Priority Badge */
.priority-section {
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid #e5e7eb;
  border-radius: 24px;
  padding: 1.5rem;
}

.priority-badge {
  display: inline-block;
  padding: 0.35rem 1rem;
  border-radius: 999px;
  font-weight: 700;
  font-size: 0.9rem;
}

.priority-high {
  background: #16a34a;
  color: #fff;
}

.priority-medium {
  background: #ca8a04;
  color: #fff;
}

.priority-low {
  background: #6b7280;
  color: #fff;
}

/* Score Components */
.components-section {
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid #e5e7eb;
  border-radius: 24px;
  padding: 1.5rem;
}

.components-list {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.component-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0.4rem 0;
  border-bottom: 1px solid #f3f4f6;
  font-size: 0.9rem;
}

.component-item:last-child {
  border-bottom: none;
}

.component-id {
  font-family: monospace;
  color: #6b7280;
  font-size: 0.8rem;
}

.component-score {
  font-weight: 600;
  color: #374151;
}

/* Recommendation */
.recommendation-section {
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid #e5e7eb;
  border-radius: 24px;
  padding: 1.5rem;
}

.recommendation-text {
  color: #374151;
  line-height: 1.6;
  margin: 0;
}

/* Assessment Cards */
.assessments {
  display: flex;
  flex-direction: column;
  gap: 0.75rem;
}

.assessment-card {
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid #e5e7eb;
  border-radius: 16px;
  overflow: hidden;
}

.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  width: 100%;
  padding: 1rem 1.25rem;
  border: none;
  background: transparent;
  cursor: pointer;
  font-size: 1rem;
  font-weight: 600;
  color: #1f2937;
  transition: background 0.15s;
}

.card-header:hover {
  background: rgba(0, 0, 0, 0.02);
}

.toggle-icon {
  font-size: 0.8rem;
  color: #9ca3af;
}

.card-body {
  padding: 0 1.25rem 1.25rem;
}

.assessment-item {
  padding: 0.75rem 0;
  border-bottom: 1px solid #f3f4f6;
}

.assessment-item:last-child {
  border-bottom: none;
}

.item-header {
  display: flex;
  align-items: flex-start;
  gap: 0.5rem;
  margin-bottom: 0.35rem;
}

.verdict {
  display: inline-block;
  padding: 0.15rem 0.5rem;
  border-radius: 6px;
  font-size: 0.75rem;
  font-weight: 700;
  white-space: nowrap;
  flex-shrink: 0;
}

.verdict.satisfied {
  background: rgba(22, 163, 74, 0.1);
  color: #16a34a;
}

.verdict.gap {
  background: rgba(220, 38, 38, 0.1);
  color: #dc2626;
}

.verdict.unknown {
  background: rgba(202, 138, 4, 0.1);
  color: #ca8a04;
}

.verdict.risk {
  background: rgba(234, 88, 12, 0.1);
  color: #ea580c;
}

.requirement-text {
  font-size: 0.9rem;
  color: #374151;
  line-height: 1.4;
}

.item-detail {
  margin: 0.35rem 0 0;
  font-size: 0.85rem;
  color: #6b7280;
  line-height: 1.5;
}

.item-evidence {
  margin-top: 0.35rem;
  font-size: 0.8rem;
  color: #9ca3af;
}

.evidence-label {
  font-weight: 600;
  margin-right: 0.25rem;
}

.evidence-ids {
  font-family: monospace;
}

/* Actions */
.actions {
  display: flex;
  justify-content: flex-end;
  padding-top: 0.5rem;
}

/* Match Failed */
.match-failed {
  background: #fef2f2;
  border: 1px solid #fecaca;
  border-radius: 24px;
  padding: 1.5rem;
  color: #b91c1c;
}

.match-failed h3 {
  color: #b91c1c;
}

.match-failed p {
  margin: 0.5rem 0 0;
  font-size: 0.9rem;
}
</style>
