import { ref, computed } from 'vue'
import { login as apiLogin, register as apiRegister, fetchMe as apiFetchMe } from '../api'

interface User {
  id: string
  nickname: string
  role: 'student' | 'admin'
}

const user = ref<User | null>(null)
export const token = ref<string | null>(localStorage.getItem('job_assistant_token'))
const loading = ref(true)

export function useAuth() {
  const isAuthenticated = computed(() => !!token.value && !!user.value)
  const isAdmin = computed(() => user.value?.role === 'admin')

  async function bootstrap() {
    const t = localStorage.getItem('job_assistant_token')
    // Reset state before checking
    user.value = null
    if (t) {
      token.value = t
      try {
        const profile = await apiFetchMe(t)
        user.value = { id: profile.account, nickname: profile.nickname, role: profile.role }
      } catch {
        token.value = null
        localStorage.removeItem('job_assistant_token')
      }
    } else {
      token.value = null
    }
    loading.value = false
  }

  async function login(account: string, password: string) {
    const resp = await apiLogin({ account, password })
    if (!resp.ok || !resp.token || !resp.profile) {
      throw new Error(resp.message || '登录失败')
    }
    token.value = resp.token
    localStorage.setItem('job_assistant_token', resp.token)
    user.value = { id: resp.profile.account, nickname: resp.profile.nickname, role: resp.profile.role }
  }

  async function register(account: string, nickname: string, password: string) {
    const resp = await apiRegister({ account, nickname, password })
    if (!resp.ok || !resp.token || !resp.profile) {
      throw new Error(resp.message || '注册失败')
    }
    token.value = resp.token
    localStorage.setItem('job_assistant_token', resp.token)
    user.value = { id: resp.profile.account, nickname: resp.profile.nickname, role: resp.profile.role }
  }

  function logout() {
    token.value = null
    user.value = null
    localStorage.removeItem('job_assistant_token')
  }

  return { user, token, loading, isAuthenticated, isAdmin, bootstrap, login, register, logout }
}
