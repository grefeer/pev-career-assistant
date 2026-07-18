<template>
  <div class="app-shell">
    <nav v-if="isAuthenticated" class="shell-nav">
      <router-link to="/matching">Match</router-link>
      <router-link to="/jobs">Jobs</router-link>
      <router-link to="/profile">Profile</router-link>
      <router-link to="/snapshots">Snapshots</router-link>
      <router-link to="/devices">Devices</router-link>
      <template v-if="isAdmin">
        <router-link to="/admin/jobs">Admin Jobs</router-link>
        <router-link to="/admin/submissions">Admin Submissions</router-link>
        <router-link to="/admin/feedbacks">Admin Feedback</router-link>
      </template>
      <span class="spacer" />
      <span v-if="user">{{ user.nickname }} ({{ user.role }})</span>
      <button @click="handleLogout">Logout</button>
    </nav>
    <main class="shell-main" :class="{ 'auth-main': !isAuthenticated }">
      <router-view v-slot="{ Component }">
        <component :is="Component" :token="token" />
      </router-view>
    </main>
  </div>
</template>

<script setup lang="ts">
import { useRouter } from 'vue-router'
import { useAuth } from '../state/auth'

const router = useRouter()
const { user, token, isAuthenticated, isAdmin, logout } = useAuth()

function handleLogout() {
  logout()
  router.push('/login')
}
</script>

<style scoped>
.app-shell {
  display: flex;
  min-height: 100vh;
  color: #1f2937;
}

.shell-nav {
  width: 240px;
  padding: 1.2rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  border-right: 1px solid rgba(229, 231, 235, 0.9);
  background: rgba(255, 255, 255, 0.7);
}

.shell-nav a {
  display: block;
  padding: 0.6rem 0.8rem;
  border-radius: 12px;
  color: #374151;
  text-decoration: none;
  font-weight: 600;
  transition: background 0.15s ease;
}

.shell-nav a:hover {
  background: rgba(15, 118, 110, 0.08);
}

.shell-nav a.router-link-active {
  background: rgba(15, 118, 110, 0.12);
  color: #0f766e;
}

.spacer {
  flex: 1;
}

.shell-nav span {
  padding: 0.4rem 0.8rem;
  color: #6b7280;
  font-size: 0.85rem;
}

.shell-nav button {
  padding: 0.6rem 0.8rem;
  border: 1px solid #fee2e2;
  border-radius: 12px;
  background: transparent;
  color: #b91c1c;
  cursor: pointer;
  font-weight: 600;
  transition: background 0.15s ease;
}

.shell-nav button:hover {
  background: #fee2e2;
}

.shell-main {
  flex: 1;
  padding: 1.6rem;
  overflow-y: auto;
}

.auth-main {
  padding: 0;
}

@media (max-width: 1100px) {
  .app-shell {
    flex-direction: column;
  }

  .shell-nav {
    width: 100%;
    flex-direction: row;
    flex-wrap: wrap;
    border-right: none;
    border-bottom: 1px solid rgba(229, 231, 235, 0.9);
    padding: 0.8rem 1.2rem;
  }

  .shell-nav .spacer {
    display: none;
  }
}
</style>
