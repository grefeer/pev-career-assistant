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
    path: '/matching',
    name: 'matching',
    component: () => import('../features/matching/MatchingWorkspace.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/resume-drafts/:draftId',
    name: 'resume-draft',
    component: () => import('../features/matching/ResumeDraftReview.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/jobs',
    name: 'jobs',
    component: () => import('../features/jobs/JobCenter.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/jobs/submissions',
    name: 'job-submissions',
    component: () => import('../features/job-submissions/JobSubmissions.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/profile',
    name: 'profile',
    component: () => import('../features/profile/ProfileWorkspace.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/snapshots',
    name: 'snapshots',
    component: () => import('../features/snapshots/SnapshotList.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/snapshots/:id',
    name: 'snapshot-detail',
    component: () => import('../features/snapshots/SnapshotDetail.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/devices',
    name: 'devices',
    component: () => import('../features/devices/DevicePlaceholder.vue'),
    meta: { requiresAuth: true },
  },
  {
    path: '/admin/jobs',
    name: 'admin-jobs',
    component: () => import('../features/jobs/AdminJobReview.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  {
    path: '/admin/submissions',
    name: 'admin-submissions',
    component: () => import('../features/job-submissions/AdminJobSubmissions.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  {
    path: '/admin/feedbacks',
    name: 'admin-feedbacks',
    component: () => import('../features/jobs/AdminJobFeedback.vue'),
    meta: { requiresAuth: true, requiresAdmin: true },
  },
  { path: '/analysis', redirect: '/matching' },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

export default router
