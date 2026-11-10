<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth.ts'
import { useToastStore } from '@/stores/toast.ts'
import Button from '@/components/ui/Button.vue'
import ToastContainer from '@/components/ui/ToastContainer.vue'
import { Lock, Eye, EyeOff, ShieldCheck, AlertCircle, Terminal } from 'lucide-vue-next'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()
const toast = useToastStore()

const inputToken = ref('')
const showPassword = ref(false)
const loading = ref(false)
const errorMsg = ref('')

/** Only allow relative in-app paths to prevent open redirect via ?redirect=//evil.com. */
function safeRedirect(raw: unknown): string {
  const value = typeof raw === 'string' ? raw : ''
  if (!value.startsWith('/') || value.startsWith('//') || value.startsWith('/\\')) {
    return '/'
  }
  return value
}

onMounted(async () => {
  if (authStore.token) {
    inputToken.value = authStore.token
    loading.value = true
    const ok = await authStore.login(authStore.token)
    loading.value = false
    if (ok) {
      router.replace(safeRedirect(route.query.redirect))
    }
  }
})

async function handleLogin() {
  const token = inputToken.value.trim()
  if (!token) {
    errorMsg.value = '请输入控制台访问密码'
    return
  }

  errorMsg.value = ''
  loading.value = true

  try {
    const ok = await authStore.login(token)
    if (ok) {
      toast.success('身份验证通过')
      router.replace(safeRedirect(route.query.redirect))
    } else {
      errorMsg.value = '管理密码校验失败，请检查 AISTUDIO_WEB_PASSWORD 配置'
      toast.error('密码错误或无权访问')
    }
  } catch {
    errorMsg.value = '网络请求异常，请确认后端网关服务在线'
    toast.error('登录请求失败')
  } finally {
    loading.value = false
  }
}
</script>

<template>
  <div class="min-h-screen bg-slate-50 flex flex-col justify-center items-center p-4 sm:p-6">
    <ToastContainer />

    <div class="w-full max-w-sm bg-white rounded-2xl shadow-lg border border-slate-200/90 p-7 sm:p-8">
      <!-- Header / Logo -->
      <div class="flex flex-col items-center text-center mb-6">
        <div class="w-11 h-11 bg-slate-900 text-white rounded-xl flex items-center justify-center mb-3.5 shadow-sm">
          <Terminal class="w-5 h-5 text-brand-400" />
        </div>
        <h1 class="text-lg font-bold text-slate-900 tracking-tight">
          AI Studio Gateway
        </h1>
        <p class="text-xs text-slate-500 mt-1">
          多协议反向代理与凭据调度控制台
        </p>
      </div>

      <!-- Error Alert -->
      <div
        v-if="errorMsg"
        class="mb-5 p-3 bg-rose-50 border border-rose-200/80 rounded-xl text-xs font-medium text-rose-800 flex items-start gap-2"
      >
        <AlertCircle class="w-4 h-4 text-rose-600 shrink-0 mt-0.5" />
        <span>{{ errorMsg }}</span>
      </div>

      <!-- Form -->
      <form
        class="space-y-4"
        @submit.prevent="handleLogin"
      >
        <div class="space-y-1.5">
          <label class="block text-xs font-semibold text-slate-700">
            管理访问密码 (Web Password)
          </label>
          <div class="relative">
            <div class="absolute inset-y-0 left-0 pl-3 flex items-center pointer-events-none text-slate-400">
              <Lock class="w-4 h-4" />
            </div>
            <input
              v-model="inputToken"
              :type="showPassword ? 'text' : 'password'"
              placeholder="输入 AISTUDIO_WEB_PASSWORD"
              autocomplete="current-password"
              autofocus
              class="w-full pl-9 pr-9 py-2.5 bg-slate-50 border border-slate-200 rounded-lg text-xs outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all font-mono"
            >
            <button
              type="button"
              class="absolute inset-y-0 right-0 pr-3 flex items-center text-slate-400 hover:text-slate-600 transition-colors cursor-pointer"
              @click="showPassword = !showPassword"
            >
              <EyeOff
                v-if="showPassword"
                class="w-4 h-4"
              />
              <Eye
                v-else
                class="w-4 h-4"
              />
            </button>
          </div>
        </div>

        <Button
          type="submit"
          variant="primary"
          size="lg"
          class="w-full justify-center text-xs font-semibold"
          :loading="loading"
          :disabled="!inputToken.trim()"
        >
          <ShieldCheck class="w-4 h-4" />
          <span>进入控制台</span>
        </Button>
      </form>
    </div>
  </div>
</template>
