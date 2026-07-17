import type { Router } from 'vue-router'
import { useAuth } from '../state/auth'

export function applyGuards(router: Router) {
  router.beforeEach(async (to, _from, next) => {
    const auth = useAuth()

    // Wait for bootstrap on first navigation
    if (auth.loading.value) {
      await new Promise<void>((resolve) => {
        const unwatch = setInterval(() => {
          if (!auth.loading.value) {
            clearInterval(unwatch)
            resolve()
          }
        }, 50)
      })
    }

    if (to.meta.requiresAuth && !auth.isAuthenticated.value) {
      next('/login')
      return
    }

    if (to.meta.requiresAdmin && !auth.isAdmin.value) {
      next('/')
      return
    }

    next()
  })
}
