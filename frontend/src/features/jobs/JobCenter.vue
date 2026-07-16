<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";

import { fetchVerifiedJob, fetchVerifiedJobs } from "./jobsApi";
import type { JobDetail, JobSummary } from "./jobTypes";

const PAGE_SIZE = 6;
const props = defineProps<{ token: string }>();

const jobs = ref<JobSummary[]>([]);
const total = ref(0);
const page = ref(1);
const loading = ref(true);
const error = ref("");
const selectedJob = ref<JobDetail | null>(null);
const selectedJobId = ref("");
const detailLoading = ref(false);
const detailError = ref("");
let isMounted = true;
let listRequestVersion = 0;
let detailRequestVersion = 0;

const filters = reactive({
  company: "",
  recruitmentType: "",
  sourceKey: "",
});

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)));
const hasPreviousPage = computed(() => page.value > 1);
const hasNextPage = computed(() => page.value * PAGE_SIZE < total.value);

function currentQuery() {
  return {
    limit: PAGE_SIZE,
    offset: (page.value - 1) * PAGE_SIZE,
    company: filters.company.trim(),
    recruitmentType: filters.recruitmentType.trim(),
    sourceKey: filters.sourceKey.trim(),
  };
}

async function loadJobs() {
  const requestVersion = ++listRequestVersion;
  detailRequestVersion += 1;
  loading.value = true;
  error.value = "";
  selectedJob.value = null;
  selectedJobId.value = "";
  detailLoading.value = false;
  detailError.value = "";
  try {
    const response = await fetchVerifiedJobs(props.token, currentQuery());
    if (!isMounted || requestVersion !== listRequestVersion) return;
    jobs.value = response.jobs;
    total.value = response.total;
  } catch (caught) {
    if (!isMounted || requestVersion !== listRequestVersion) return;
    jobs.value = [];
    total.value = 0;
    error.value = caught instanceof Error ? caught.message : "职位加载失败。";
  } finally {
    if (isMounted && requestVersion === listRequestVersion) {
      loading.value = false;
    }
  }
}

async function applyFilters() {
  page.value = 1;
  await loadJobs();
}

async function clearFilters() {
  filters.company = "";
  filters.recruitmentType = "";
  filters.sourceKey = "";
  page.value = 1;
  await loadJobs();
}

async function changePage(nextPage: number) {
  page.value = nextPage;
  await loadJobs();
}

async function showDetail(jobId: string) {
  const requestVersion = ++detailRequestVersion;
  selectedJobId.value = jobId;
  selectedJob.value = null;
  detailError.value = "";
  detailLoading.value = true;
  try {
    const response = await fetchVerifiedJob(props.token, jobId);
    if (!isMounted || requestVersion !== detailRequestVersion) return;
    selectedJob.value = response;
  } catch (caught) {
    if (!isMounted || requestVersion !== detailRequestVersion) return;
    detailError.value = caught instanceof Error ? caught.message : "职位详情加载失败。";
  } finally {
    if (isMounted && requestVersion === detailRequestVersion) {
      detailLoading.value = false;
    }
  }
}

function closeDetail() {
  detailRequestVersion += 1;
  selectedJobId.value = "";
  selectedJob.value = null;
  detailLoading.value = false;
  detailError.value = "";
}

function formatDate(value: string | null) {
  return value ? value.slice(0, 10) : "未注明";
}

onMounted(loadJobs);
onUnmounted(() => {
  isMounted = false;
  listRequestVersion += 1;
  detailRequestVersion += 1;
});
</script>

<template>
  <section class="job-center" aria-labelledby="job-center-title">
    <header class="job-header">
      <div>
        <p class="eyebrow">VERIFIED JOBS</p>
        <h2 id="job-center-title">已核验职位</h2>
        <p>每个职位均完成人工核验，信息更具体，投递入口更可靠。</p>
      </div>
      <div class="verified-mark" aria-label="人工核验">
        <span aria-hidden="true">✓</span>
        人工核验
      </div>
    </header>

    <form class="job-filters" aria-label="职位筛选" @submit.prevent="applyFilters">
      <label>
        公司
        <input
          v-model="filters.company"
          data-test="company-filter"
          placeholder="例如：示例科技"
        />
      </label>
      <label>
        招聘类型
        <input
          v-model="filters.recruitmentType"
          data-test="type-filter"
          placeholder="例如：实习"
        />
      </label>
      <label>
        来源
        <input v-model="filters.sourceKey" data-test="source-filter" placeholder="来源标识" />
      </label>
      <div class="filter-actions">
        <button class="primary-action" type="submit">筛选职位</button>
        <button class="secondary-action" type="button" @click="clearFilters">清除</button>
      </div>
    </form>

    <p v-if="loading" class="state-card" role="status">正在加载职位…</p>
    <div v-else-if="error" class="state-card error-state" role="alert">
      <strong>职位加载失败</strong>
      <span>{{ error }}</span>
      <button data-test="retry-jobs" type="button" @click="loadJobs">重新加载</button>
    </div>
    <div v-else-if="jobs.length === 0" class="state-card empty-state">
      <strong>当前没有符合条件的已核验职位。</strong>
      <span>可以调整筛选条件后再试。</span>
    </div>

    <template v-else>
      <div class="result-meta">
        <span>共 {{ total }} 个职位</span>
        <span>第 {{ page }} / {{ totalPages }} 页</span>
      </div>
      <div class="job-grid">
        <article v-for="job in jobs" :key="job.id" class="job-card">
          <div class="card-heading">
            <div>
              <p class="job-company">{{ job.company_name }}</p>
              <h3>{{ job.title }}</h3>
            </div>
            <span class="source-chip">{{ job.source_name }}</span>
          </div>

          <dl>
            <div><dt>地点</dt><dd>{{ job.locations.join("、") || "未注明" }}</dd></div>
            <div><dt>类型</dt><dd>{{ job.recruitment_types.join("、") || "未注明" }}</dd></div>
            <div><dt>行业</dt><dd>{{ job.industries.join("、") || "未注明" }}</dd></div>
            <div><dt>截止</dt><dd>{{ job.deadline_text || "未注明" }}</dd></div>
          </dl>

          <p class="eligibility" :class="{ manual: !job.gui_eligible }">
            {{ job.gui_eligible ? "可使用辅助填写" : "仅支持人工投递" }}
          </p>

          <div class="card-actions">
            <button
              class="secondary-action"
              :data-test="`show-detail-${job.id}`"
              type="button"
              @click="showDetail(job.id)"
            >
              查看职位详情
            </button>
            <a :href="job.apply_url" target="_blank" rel="noopener noreferrer">
              打开官方入口
            </a>
          </div>
        </article>
      </div>

      <nav class="pagination" aria-label="职位分页">
        <button
          class="secondary-action"
          data-test="previous-page"
          type="button"
          :disabled="!hasPreviousPage"
          @click="changePage(page - 1)"
        >
          上一页
        </button>
        <span>第 {{ page }} / {{ totalPages }} 页</span>
        <button
          class="secondary-action"
          data-test="next-page"
          type="button"
          :disabled="!hasNextPage"
          @click="changePage(page + 1)"
        >
          下一页
        </button>
      </nav>
    </template>

    <aside v-if="selectedJobId" class="job-detail" aria-live="polite">
      <p v-if="detailLoading" role="status">正在加载职位详情…</p>
      <div v-else-if="detailError" role="alert">
        <strong>详情加载失败</strong>
        <p>{{ detailError }}</p>
        <button class="secondary-action" type="button" @click="showDetail(selectedJobId)">
          重新加载详情
        </button>
      </div>
      <article v-else-if="selectedJob">
        <div class="detail-heading">
          <div>
            <p class="job-company">{{ selectedJob.company_name }}</p>
            <h3>{{ selectedJob.title }}</h3>
          </div>
          <button class="close-button" type="button" aria-label="关闭职位详情" @click="closeDetail">
            ×
          </button>
        </div>
        <div class="detail-body">
          <section>
            <h4>职位描述</h4>
            <p class="description">{{ selectedJob.description_text }}</p>
          </section>
          <dl>
            <div><dt>内推码</dt><dd>{{ selectedJob.referral_code || "无" }}</dd></div>
            <div><dt>核验日期</dt><dd>{{ formatDate(selectedJob.verified_at) }}</dd></div>
            <div><dt>更新日期</dt><dd>{{ formatDate(selectedJob.updated_at) }}</dd></div>
          </dl>
        </div>
      </article>
    </aside>
  </section>
</template>

<style scoped>
.job-center {
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
  background:
    linear-gradient(125deg, rgba(226, 238, 229, 0.72), transparent 40%),
    var(--paper);
  border: 1px solid var(--line);
  border-radius: 26px;
  box-shadow: 0 20px 50px rgba(28, 71, 56, 0.09);
}

.job-header,
.card-heading,
.detail-heading,
.result-meta,
.pagination,
.card-actions,
.filter-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
}

.job-header h2,
.job-card h3,
.job-detail h3,
.job-detail h4,
.job-header p {
  margin: 0;
}

.job-header h2 {
  font-family: Georgia, "Songti SC", serif;
  font-size: clamp(2rem, 4vw, 3rem);
  line-height: 1.08;
}

.job-header > div > p:last-child {
  margin-top: 0.6rem;
  color: var(--muted);
}

.eyebrow {
  color: var(--pine);
  font-size: 0.76rem;
  font-weight: 800;
  letter-spacing: 0.16em;
}

.verified-mark,
.source-chip,
.eligibility {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  width: fit-content;
  border-radius: 999px;
  font-size: 0.8rem;
  font-weight: 700;
}

.verified-mark {
  flex: 0 0 auto;
  padding: 0.6rem 0.85rem;
  color: var(--pine-dark);
  background: #e3f2e9;
  border: 1px solid #c6dfce;
}

.job-filters {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr)) auto;
  gap: 0.8rem;
  padding: 1rem;
  background: rgba(255, 255, 255, 0.72);
  border: 1px solid var(--line);
  border-radius: 18px;
}

.job-filters label {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  color: var(--muted);
  font-size: 0.78rem;
  font-weight: 700;
}

.job-filters input {
  min-width: 0;
  padding: 0.72rem 0.8rem;
  color: var(--ink);
  background: white;
  border: 1px solid var(--line);
  border-radius: 11px;
  outline: none;
}

.job-filters input:focus {
  border-color: var(--pine);
  box-shadow: 0 0 0 3px rgba(28, 102, 80, 0.12);
}

button,
a {
  transition: transform 140ms ease, box-shadow 140ms ease, background 140ms ease;
}

button {
  font: inherit;
  cursor: pointer;
}

button:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.primary-action,
.secondary-action,
.card-actions a,
.error-state button {
  padding: 0.68rem 0.85rem;
  border-radius: 11px;
  font-size: 0.86rem;
  font-weight: 750;
  text-decoration: none;
}

.primary-action,
.card-actions a,
.error-state button {
  color: white;
  background: var(--pine);
  border: 1px solid var(--pine);
}

.secondary-action {
  color: var(--pine-dark);
  background: #f8fbf8;
  border: 1px solid #cddbd2;
}

.primary-action:hover,
.secondary-action:hover:not(:disabled),
.card-actions a:hover,
.error-state button:hover {
  transform: translateY(-1px);
  box-shadow: 0 8px 18px rgba(28, 71, 56, 0.13);
}

.state-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 0.5rem;
  min-height: 8rem;
  margin: 0;
  padding: 1.4rem;
  justify-content: center;
  color: var(--muted);
  background: rgba(255, 255, 255, 0.75);
  border: 1px dashed #cbd8cf;
  border-radius: 18px;
}

.error-state {
  color: #8c2929;
  background: #fff8f5;
  border-color: #eac4bd;
}

.result-meta {
  color: var(--muted);
  font-size: 0.86rem;
}

.job-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 1rem;
}

.job-card {
  display: flex;
  flex-direction: column;
  gap: 1rem;
  min-width: 0;
  padding: 1.25rem;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid var(--line);
  border-radius: 18px;
}

.job-card:hover {
  border-color: #b9d0c1;
  box-shadow: 0 14px 28px rgba(28, 71, 56, 0.08);
}

.job-company {
  margin: 0 0 0.25rem;
  color: var(--pine);
  font-size: 0.86rem;
  font-weight: 800;
}

.job-card h3,
.job-detail h3 {
  font-family: Georgia, "Songti SC", serif;
  font-size: 1.3rem;
  line-height: 1.25;
}

.source-chip {
  flex: 0 0 auto;
  padding: 0.3rem 0.55rem;
  color: #56645f;
  background: #f1f4f1;
  border: 1px solid #dde4df;
}

dl {
  display: grid;
  gap: 0.65rem;
  margin: 0;
}

dl div {
  display: grid;
  grid-template-columns: 3rem 1fr;
  gap: 0.75rem;
}

dt {
  color: var(--muted);
  font-size: 0.8rem;
}

dd {
  margin: 0;
  overflow-wrap: anywhere;
}

.eligibility {
  margin: auto 0 0;
  padding: 0.36rem 0.62rem;
  color: #1a624d;
  background: #e6f4eb;
}

.eligibility.manual {
  color: #7b5722;
  background: #fff1d9;
}

.card-actions {
  align-items: stretch;
}

.card-actions > * {
  flex: 1;
  text-align: center;
}

.pagination {
  justify-content: center;
  color: var(--muted);
}

.job-detail {
  padding: 1.3rem;
  background: #f4f8f4;
  border: 1px solid #cadace;
  border-radius: 18px;
}

.detail-heading {
  align-items: flex-start;
  padding-bottom: 1rem;
  border-bottom: 1px solid #d5e1d8;
}

.close-button {
  width: 2.25rem;
  height: 2.25rem;
  color: var(--muted);
  background: white;
  border: 1px solid var(--line);
  border-radius: 50%;
  font-size: 1.35rem;
}

.detail-body {
  display: grid;
  grid-template-columns: minmax(0, 2fr) minmax(200px, 1fr);
  gap: 1.5rem;
  padding-top: 1rem;
}

.description {
  margin: 0.6rem 0 0;
  white-space: pre-wrap;
  line-height: 1.75;
}

@media (max-width: 900px) {
  .job-filters,
  .detail-body {
    grid-template-columns: 1fr;
  }

  .filter-actions {
    justify-content: flex-start;
  }
}

@media (max-width: 600px) {
  .job-header,
  .card-heading,
  .card-actions {
    align-items: flex-start;
    flex-direction: column;
  }

  .job-grid {
    grid-template-columns: 1fr;
  }

  .card-actions > * {
    width: 100%;
  }
}
</style>
