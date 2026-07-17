<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useAuth } from '../state/auth'

const router = useRouter()
const { login, register } = useAuth()

const authMode = ref<'login' | 'register'>('login')
const loading = ref(false)
const errorMessage = ref('')
const successMessage = ref('')

const form = reactive({
  account: '',
  nickname: '',
  password: '',
})

function setSuccess(message: string) {
  successMessage.value = message
  errorMessage.value = ''
}

function setError(message: string) {
  errorMessage.value = message
  successMessage.value = ''
}

async function handleAuth() {
  loading.value = true
  errorMessage.value = ''
  successMessage.value = ''
  try {
    if (authMode.value === 'login') {
      await login(form.account, form.password)
      setSuccess('登录成功')
    } else {
      await register(form.account, form.nickname, form.password)
      setSuccess('注册成功')
    }
    form.password = ''
    router.push('/')
  } catch (error: any) {
    setError(error.message || '认证失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="auth-layout">
    <section class="hero-card">
      <p class="eyebrow">Vue + FastAPI Upgrade</p>
      <h1>把 LangGraph 多智能体求职助手升级成前后端分离项目。</h1>
      <p class="subtitle">
        现在前端负责账户、会话、上传和展示，后端负责 LangGraph 工作流、SQLite checkpoint 和分析接口。
      </p>
      <div class="hero-grid">
        <div class="metric-card">
          <span>后端</span>
          <strong>FastAPI</strong>
        </div>
        <div class="metric-card">
          <span>前端</span>
          <strong>Vue 3 + Vite</strong>
        </div>
        <div class="metric-card">
          <span>状态持久化</span>
          <strong>SQLite checkpoint</strong>
        </div>
        <div class="metric-card">
          <span>核心能力</span>
          <strong>Command / Send / 子图</strong>
        </div>
      </div>
    </section>

    <section class="auth-card">
      <div class="tab-switch">
        <button :class="{ active: authMode === 'login' }" @click="authMode = 'login'">登录</button>
        <button :class="{ active: authMode === 'register' }" @click="authMode = 'register'">注册</button>
      </div>

      <div class="field">
        <label>账号</label>
        <input v-model="form.account" placeholder="例如 lichunfeng" />
      </div>

      <div class="field" v-if="authMode === 'register'">
        <label>昵称</label>
        <input v-model="form.nickname" placeholder="页面中显示的名字" />
      </div>

      <div class="field">
        <label>密码</label>
        <input v-model="form.password" type="password" placeholder="至少 6 位" />
      </div>

      <button class="primary-button" :disabled="loading" @click="handleAuth">
        {{ loading ? '处理中...' : authMode === 'login' ? '进入工作台' : '注册并进入' }}
      </button>

      <p v-if="successMessage" class="feedback success">{{ successMessage }}</p>
      <p v-if="errorMessage" class="feedback error">{{ errorMessage }}</p>
    </section>
  </main>
</template>

<style scoped>
.auth-layout {
  width: 100%;
  min-height: 100vh;
  padding: 3rem;
  display: grid;
  grid-template-columns: 1.2fr 0.8fr;
  gap: 2rem;
  align-content: center;
}

.hero-card,
.auth-card {
  background: rgba(255, 255, 255, 0.92);
  border: 1px solid rgba(229, 231, 235, 0.9);
  border-radius: 24px;
  box-shadow: 0 18px 42px rgba(15, 23, 42, 0.08);
  padding: 1.5rem;
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

.hero-card h1 {
  font-size: clamp(2rem, 4vw, 3.1rem);
  line-height: 1.1;
  margin: 0 0 1rem;
}

.subtitle {
  color: #6b7280;
  line-height: 1.8;
}

.hero-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 1rem;
  margin-top: 1.2rem;
}

.metric-card {
  background: #fafaf9;
  border: 1px solid #e5e7eb;
  border-radius: 18px;
  padding: 1rem;
}

.metric-card span {
  display: block;
  color: #6b7280;
  margin-bottom: 0.35rem;
}

.metric-card strong {
  font-size: 1.3rem;
}

.tab-switch {
  display: flex;
  gap: 0.75rem;
  margin-bottom: 1rem;
}

.tab-switch button {
  flex: 1;
  background: #f3f4f6;
  color: #374151;
  border: none;
  border-radius: 16px;
  padding: 0.8rem 1rem;
  cursor: pointer;
  transition: transform 0.15s ease, box-shadow 0.15s ease;
}

.tab-switch button.active {
  background: linear-gradient(135deg, #0f766e, #14b8a6);
  color: white;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
  margin-bottom: 1rem;
}

.field label {
  font-size: 0.92rem;
  font-weight: 700;
}

.field input {
  width: 100%;
  border: 1px solid #dbe3ea;
  border-radius: 16px;
  padding: 0.85rem 1rem;
  background: #fbfcfd;
  box-sizing: border-box;
}

.primary-button {
  background: linear-gradient(135deg, #0f766e, #14b8a6);
  color: white;
  width: 100%;
  border: none;
  border-radius: 16px;
  padding: 0.8rem 1rem;
  cursor: pointer;
  transition: transform 0.15s ease, box-shadow 0.15s ease;
  font-size: 1rem;
}

.primary-button:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

.primary-button:hover:not(:disabled) {
  transform: translateY(-1px);
  box-shadow: 0 12px 24px rgba(15, 23, 42, 0.08);
}

.feedback {
  margin-top: 0.9rem;
  border-radius: 16px;
  padding: 0.8rem 1rem;
}

.success {
  background: #dcfce7;
  color: #166534;
}

.error {
  background: #fee2e2;
  color: #991b1b;
}

@media (max-width: 1100px) {
  .auth-layout {
    grid-template-columns: 1fr;
  }
}
</style>
