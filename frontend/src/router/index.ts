import { createRouter, createWebHistory } from 'vue-router'

export const routes = [
  {
    path: '/login',
    name: 'login',
    component: () => import('../components/LoginPage.vue'),
  },
  { path: '/', redirect: '/assistant' },
  {
    path: '/assistant',
    name: 'assistant',
    component: () => import('../features/agent-workspace/AgentWorkspace.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/profile',
    name: 'profile',
    component: () => import('../features/profile/ProfileWorkspace.vue'),
    meta: { requiresAuth: true },
  },
  { path: '/:pathMatch(.*)*', redirect: '/assistant' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
