import { ref } from 'vue'
import { fetchSessions as apiFetchSessions, activateSession as apiActivateSession } from '../api'
import { token } from './auth'

export function useSessions() {
  const sessions = ref<any[]>([])
  const currentSessionId = ref<string | null>(null)

  async function load() {
    if (!token.value) return
    const resp = await apiFetchSessions(token.value)
    sessions.value = resp.sessions
    if (!currentSessionId.value) {
      currentSessionId.value = resp.active_thread_id || resp.sessions[0]?.thread_id || null
    }
  }

  async function select(id: string) {
    if (!token.value) return
    await apiActivateSession(token.value, id)
    currentSessionId.value = id
  }

  return { sessions, currentSessionId, load, select }
}
