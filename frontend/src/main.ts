import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import { applyGuards } from './router/guards'
import { useAuth } from './state/auth'
import './styles.css'

async function main() {
  const app = createApp(App)

  applyGuards(router)
  app.use(router)

  const auth = useAuth()
  await auth.bootstrap()

  app.mount('#app')
}

main()
