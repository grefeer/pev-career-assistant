<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { useAuth } from "../../state/auth";
import { listSnapshots } from "./snapshotApi";
import type { SnapshotSummary } from "./snapshotTypes";

const { token } = useAuth();
const router = useRouter();

const snapshots = ref<SnapshotSummary[]>([]);
const loading = ref(true);
const errorMessage = ref("");

onMounted(async () => {
  await loadSnapshots();
});

async function loadSnapshots() {
  if (!token.value) return;
  loading.value = true;
  errorMessage.value = "";
  try {
    const result = await listSnapshots(token.value);
    snapshots.value = result.items;
  } catch (err: any) {
    errorMessage.value = err.message || "Failed to load snapshots.";
  } finally {
    loading.value = false;
  }
}

function viewDetail(id: string) {
  router.push(`/snapshots/${encodeURIComponent(id)}`);
}

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
  <div class="snapshot-list">
    <div class="header">
      <span class="eyebrow">APPLICATION SNAPSHOTS</span>
      <h2>Snapshots</h2>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="loading">
      <p>Loading snapshots...</p>
    </div>

    <!-- Error -->
    <div v-else-if="errorMessage" class="error-banner">
      <p>{{ errorMessage }}</p>
      <button class="btn btn-outline" @click="loadSnapshots">Retry</button>
    </div>

    <!-- Empty state -->
    <div v-else-if="snapshots.length === 0" class="empty-state">
      <p>No snapshots yet. Approve a resume draft and create a snapshot to get started.</p>
      <router-link to="/matching" class="btn btn-outline">Go to Matching</router-link>
    </div>

    <!-- Snapshot table -->
    <table v-else class="snapshot-table">
      <thead>
        <tr>
          <th>Company</th>
          <th>Title</th>
          <th>Created</th>
          <th>GUI Eligible</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="snap in snapshots"
          :key="snap.id"
          class="snapshot-row"
          @click="viewDetail(snap.id)"
        >
          <td class="cell-company">{{ snap.company_name }}</td>
          <td class="cell-title">{{ snap.title }}</td>
          <td class="cell-date">{{ formatDate(snap.created_at) }}</td>
          <td class="cell-eligible">
            <span
              v-if="snap.gui_eligible"
              class="badge badge-yes"
            >Yes</span>
            <span
              v-else
              class="badge badge-no"
            >No</span>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.snapshot-list {
  max-width: 1000px;
  margin: 0 auto;
  padding: 1.5rem;
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
  margin: 0;
}

.loading {
  text-align: center;
  padding: 3rem 1rem;
  color: #6b7280;
}

.error-banner {
  padding: 0.75rem 1rem;
  border-radius: 12px;
  background: #fee2e2;
  border: 1px solid #fecaca;
  color: #991b1b;
  margin-bottom: 1rem;
  display: flex;
  align-items: center;
  gap: 1rem;
  font-size: 0.9rem;
}

.empty-state {
  text-align: center;
  padding: 3rem 1rem;
  color: #6b7280;
}

.empty-state p {
  margin-bottom: 1rem;
}

/* ── Table ──────────────────────────────────────────────────────────────────── */
.snapshot-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid #e5e7eb;
  border-radius: 16px;
  overflow: hidden;
}

.snapshot-table th {
  text-align: left;
  padding: 0.75rem 1rem;
  font-size: 0.75rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #6b7280;
  background: #f9fafb;
  border-bottom: 1px solid #e5e7eb;
}

.snapshot-table td {
  padding: 0.75rem 1rem;
  font-size: 0.9rem;
  border-bottom: 1px solid #f3f4f6;
}

.snapshot-table tbody tr:last-child td {
  border-bottom: none;
}

.snapshot-row {
  cursor: pointer;
  transition: background 0.12s;
}

.snapshot-row:hover {
  background: #f0fdf4;
}

.cell-company {
  font-weight: 600;
  color: #1f2937;
}

.cell-title {
  color: #374151;
}

.cell-date {
  color: #6b7280;
  font-size: 0.85rem;
}

/* ── Badges ─────────────────────────────────────────────────────────────────── */
.badge {
  display: inline-block;
  padding: 0.15rem 0.55rem;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 600;
}

.badge-yes {
  background: #d1fae5;
  color: #065f46;
}

.badge-no {
  background: #f3f4f6;
  color: #6b7280;
}

/* ── Buttons ────────────────────────────────────────────────────────────────── */
.btn {
  display: inline-flex;
  align-items: center;
  padding: 0.5rem 1.2rem;
  border: none;
  border-radius: 10px;
  font-weight: 600;
  font-size: 0.85rem;
  cursor: pointer;
  text-decoration: none;
  transition: background 0.15s;
}

.btn-outline {
  background: transparent;
  border: 1px solid #d1d5db;
  color: #374151;
}

.btn-outline:hover {
  background: #f3f4f6;
}
</style>
