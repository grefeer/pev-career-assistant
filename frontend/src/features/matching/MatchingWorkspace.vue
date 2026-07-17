<script setup lang="ts">
import { useAuth } from '../../state/auth'
import { useSessions } from '../../state/sessions'
const { user, token } = useAuth()
const { sessions, currentSessionId, load, select } = useSessions()
</script>

<template>
  <div class="matching-placeholder">
    <p class="eyebrow">MATCHING WORKSPACE</p>
    <h2>分析工作台</h2>
    <p class="subtitle">此功能将在后续迭代中完善。现有分析功能已在旧版 App.vue 中迁移。</p>
    <div class="info-card">
      <p>当前用户：{{ user?.nickname }}</p>
      <p>活跃会话：{{ currentSessionId || '未选择' }}</p>
      <p>会话数：{{ sessions.length }}</p>
    </div>
  </div>
</template>

<style scoped>
.matching-placeholder {
  padding: 2rem;
}
.eyebrow {
  display: inline-block;
  padding: 0.3rem 0.65rem;
  border-radius: 999px;
  background: rgba(15, 118, 110, 0.1);
  color: #0f766e;
  font-size: 0.8rem;
  font-weight: 700;
  margin-bottom: 1rem;
}
h2 {
  font-size: clamp(1.5rem, 3vw, 2rem);
  margin: 0 0 0.5rem;
}
.subtitle {
  color: #6b7280;
}
.info-card {
  margin-top: 1rem;
  padding: 1rem;
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid #e5e7eb;
  border-radius: 24px;
}
</style>
