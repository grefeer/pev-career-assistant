<script setup lang="ts">
import { onMounted, ref } from "vue";
import { useAuth } from "../../state/auth";
import { listActiveDevices } from "./deviceApi";
import type { DeviceSummary } from "./deviceTypes";

const { token } = useAuth();

const devices = ref<DeviceSummary[]>([]);
const loading = ref(true);
const errorMessage = ref("");

onMounted(async () => {
  await loadDevices();
});

async function loadDevices() {
  if (!token.value) return;
  loading.value = true;
  errorMessage.value = "";
  try {
    const result = await listActiveDevices(token.value);
    devices.value = result.devices;
  } catch (err: any) {
    errorMessage.value = err.message || "Failed to load devices.";
  } finally {
    loading.value = false;
  }
}

function formatDate(dateStr: string | null): string {
  if (!dateStr) return "—";
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
  <div class="device-list-page">
    <div class="header">
      <span class="eyebrow">DEVICES</span>
      <h2>My Devices</h2>
    </div>

    <!-- Loading -->
    <div v-if="loading" class="loading">
      <p>Loading devices...</p>
    </div>

    <!-- Error -->
    <div v-else-if="errorMessage" class="error-banner">
      <p>{{ errorMessage }}</p>
      <button class="btn btn-outline" @click="loadDevices">Retry</button>
    </div>

    <!-- Empty -->
    <div v-else-if="devices.length === 0" class="empty-state">
      <p>No devices paired yet. Use the CLI or mobile app to pair a device.</p>
    </div>

    <!-- Device table -->
    <table v-else class="device-table">
      <thead>
        <tr>
          <th>Name</th>
          <th>Platform</th>
          <th>Status</th>
          <th>Online</th>
          <th>Version</th>
          <th>Paired At</th>
          <th>Last Seen</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="device in devices" :key="device.id" class="device-row">
          <td class="cell-name">{{ device.name }}</td>
          <td>{{ device.platform }}</td>
          <td>
            <span :class="['badge', `badge-${device.status}`]">
              {{ device.status }}
            </span>
          </td>
          <td>
            <span :class="['badge', device.online ? 'badge-online' : 'badge-offline']">
              {{ device.online ? "Online" : "Offline" }}
            </span>
          </td>
          <td class="cell-version">{{ device.version || "—" }}</td>
          <td class="cell-date">{{ formatDate(device.paired_at) }}</td>
          <td class="cell-date">{{ formatDate(device.last_seen_at) }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.device-list-page {
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

/* ── Table ──────────────────────────────────────────────────────────────────── */
.device-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid #e5e7eb;
  border-radius: 16px;
  overflow: hidden;
  font-size: 0.9rem;
}

.device-table th {
  text-align: left;
  padding: 0.75rem 0.8rem;
  font-size: 0.75rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #6b7280;
  background: #f9fafb;
  border-bottom: 1px solid #e5e7eb;
}

.device-table td {
  padding: 0.7rem 0.8rem;
  border-bottom: 1px solid #f3f4f6;
}

.device-table tbody tr:last-child td {
  border-bottom: none;
}

.device-row:hover {
  background: #f9fafb;
}

.cell-name {
  font-weight: 600;
  color: #1f2937;
}

.cell-version {
  color: #6b7280;
  font-size: 0.85rem;
}

.cell-date {
  color: #9ca3af;
  font-size: 0.8rem;
  white-space: nowrap;
}

/* ── Badges ─────────────────────────────────────────────────────────────────── */
.badge {
  display: inline-block;
  padding: 0.15rem 0.55rem;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 600;
}

.badge-active { background: #d1fae5; color: #065f46; }
.badge-inactive { background: #fef3c7; color: #92400e; }
.badge-revoked { background: #fee2e2; color: #991b1b; }
.badge-online { background: #d1fae5; color: #065f46; }
.badge-offline { background: #f3f4f6; color: #6b7280; }

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
