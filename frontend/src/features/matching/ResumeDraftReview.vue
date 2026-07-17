<script setup lang="ts">
import { useRoute } from 'vue-router'
const route = useRoute()
</script>

<template>
  <div class="placeholder">
    <h2>简历草稿审核</h2>
    <p>草稿 ID：{{ route.params.draftId }}</p>
    <p class="subtitle">此功能将在后续迭代中完善。</p>
  </div>
</template>

<style scoped>
.placeholder { padding: 2rem; }
.subtitle { color: #6b7280; }
</style>
