<script setup lang="ts">
import { computed } from 'vue'
import {
  LayoutDashboard,
  Users,
  Sliders,
  Key,
  LogOut,
  ScrollText,
  Terminal,
  Network,
  ListChecks,
  Copy,
  Check,
} from 'lucide-vue-next'
import { useAuthStore } from '@/stores/auth.ts'
import { useAccountsStore } from '@/stores/accounts.ts'
import { useRouter, useRoute } from 'vue-router'
import { useSystemHealth } from '@/composables/useSystemHealth.ts'
import { useClipboard } from '@/composables/useClipboard.ts'

defineProps<{
  open: boolean
}>()

const emit = defineEmits<{
  'update:open': [val: boolean]
  openTokenModal: []
}>()

const authStore = useAuthStore()
const accountsStore = useAccountsStore()
const router = useRouter()
const route = useRoute()
const { state: healthState, readiness } = useSystemHealth(30000)
const { copied, copy } = useClipboard()

const protocolEndpoints = [
  { id: 'gemini', label: 'Gemini', path: '/v1beta', copyPath: '/v1beta' },
  { id: 'openai', label: 'OpenAI', path: '/v1/chat/*', copyPath: '/v1' },
  { id: 'claude', label: 'Claude', path: '/v1/messages', copyPath: '' },
]

function copyEndpoint(item: { id: string; label: string; copyPath: string }) {
  const origin = typeof window !== 'undefined' && window.location.origin ? window.location.origin : 'http://localhost:8080'
  const fullUrl = `${origin}${item.copyPath}`
  void copy(fullUrl, item.id, `已复制 ${item.label} Base URL: ${fullUrl}`)
}

const accountCountBadge = computed(() => {
  const total = accountsStore.accountRows.length
  if (!total) return ''
  const ready = accountsStore.accountRows.filter(
    (a) =>
      a.enabled !== false && a.is_available !== false && a.availability !== 'partial' && !a.session_expired,
  ).length
  return `${ready}/${total}`
})

const navItems = computed(() => [
  {
    name: '控制面板',
    path: '/',
    icon: LayoutDashboard,
    badge: '',
  },
  {
    name: '账号凭据池',
    path: '/accounts',
    icon: Users,
    badge: accountCountBadge.value,
  },
  { name: '请求记录', path: '/requests', icon: ListChecks, badge: '' },
  { name: '模型请求日志', path: '/logs', icon: ScrollText, badge: '实时' },
  { name: '代理与出口', path: '/proxies', icon: Network, badge: '' },
  {
    name: '系统与协议配置',
    path: '/settings',
    icon: Sliders,
    badge: '',
  },
])

function handleNav(path: string) {
  router.push(path)
  emit('update:open', false)
}

function handleLogout() {
  authStore.logout()
  router.push('/login')
}
</script>

<template>
  <!-- Mobile Backdrop -->
  <div
    v-if="open"
    class="fixed inset-0 z-40 bg-slate-900/40 backdrop-blur-xs lg:hidden transition-opacity"
    @click="emit('update:open', false)"
  />

  <!-- Sidebar -->
  <aside
    :class="[
      'fixed top-0 bottom-0 left-0 z-40 w-60 bg-white border-r border-slate-200/90 flex flex-col transition-transform duration-200 ease-in-out lg:translate-x-0 select-none',
      open ? 'translate-x-0 shadow-2xl lg:shadow-none' : '-translate-x-full',
    ]"
  >
    <!-- Brand Identity Header -->
    <div class="h-14 px-4 border-b border-slate-100 flex items-center justify-between">
      <div class="flex items-center gap-2.5 min-w-0">
        <div
          class="w-7 h-7 rounded-lg bg-slate-900 text-white flex items-center justify-center shadow-2xs shrink-0"
        >
          <Terminal class="w-3.5 h-3.5 text-brand-400" />
        </div>
        <div class="min-w-0">
          <div class="font-bold text-[13px] text-slate-900 tracking-tight truncate leading-tight">
            AI Studio Gateway
          </div>
          <div class="text-[10px] font-mono text-slate-400 tracking-tight truncate">
            v1beta · OpenAI · Claude
          </div>
        </div>
      </div>
    </div>

    <!-- Navigation -->
    <div class="flex-1 px-3 py-4 space-y-5 overflow-y-auto">
      <div class="space-y-1">
        <div class="px-2.5 pb-1 text-[10px] font-semibold uppercase tracking-wider text-slate-400">
          核心控制台
        </div>
        <button
          v-for="item in navItems"
          :key="item.path"
          type="button"
          :class="[
            'w-full group relative flex items-center justify-between gap-2.5 px-3 py-2 rounded-lg text-[13px] transition-all text-left cursor-pointer',
            route.path === item.path
              ? 'bg-slate-900 text-white font-medium shadow-2xs'
              : 'text-slate-600 hover:bg-slate-100/80 hover:text-slate-900',
          ]"
          @click="handleNav(item.path)"
        >
          <div class="flex items-center gap-2.5 min-w-0">
            <component
              :is="item.icon"
              class="w-4 h-4 shrink-0 transition-colors"
              :class="
                route.path === item.path ? 'text-brand-400' : 'text-slate-400 group-hover:text-slate-600'
              "
            />
            <span class="truncate">{{ item.name }}</span>
          </div>

          <span
            v-if="item.badge"
            class="px-1.5 py-0.5 text-[10px] font-mono rounded tabular-nums shrink-0"
            :class="route.path === item.path ? 'bg-slate-800 text-slate-200' : 'bg-slate-100 text-slate-500'"
          >
            {{ item.badge }}
          </span>
        </button>
      </div>

      <!-- Protocol Endpoint Quick Reference -->
      <div class="px-2.5 pt-2 border-t border-slate-100 space-y-2">
        <div class="text-[10px] font-semibold uppercase tracking-wider text-slate-400">
          已挂载协议端点
        </div>
        <div class="space-y-1.5 font-mono text-[11px]">
          <button
            v-for="ep in protocolEndpoints"
            :key="ep.id"
            type="button"
            class="w-full group flex items-center justify-between px-2 py-1 rounded-md bg-slate-50 hover:bg-slate-100/90 border border-slate-200/60 hover:border-slate-300/80 text-slate-600 transition-colors cursor-pointer text-left"
            :title="`点击复制 ${ep.label} Base URL`"
            @click="copyEndpoint(ep)"
          >
            <span class="text-slate-400">{{ ep.label }}</span>
            <span class="flex items-center gap-1.5 text-slate-800 font-medium">
              <span>{{ ep.path }}</span>
              <Check
                v-if="copied === ep.id"
                class="w-3 h-3 text-emerald-500 shrink-0"
              />
              <Copy
                v-else
                class="w-3 h-3 text-slate-300 group-hover:text-slate-500 transition-colors shrink-0"
              />
            </span>
          </button>
        </div>
      </div>
    </div>

    <!-- Footer Status & Security Controls -->
    <div class="p-3 border-t border-slate-100 bg-slate-50/50 space-y-1.5">
      <button
        v-if="authStore.authEnabled"
        type="button"
        class="w-full flex items-center justify-between px-2.5 py-1.5 rounded-lg text-xs font-medium text-slate-600 hover:bg-white hover:shadow-2xs border border-transparent hover:border-slate-200/80 transition-all cursor-pointer"
        @click="emit('openTokenModal')"
      >
        <div class="flex items-center gap-2">
          <Key class="w-3.5 h-3.5 text-slate-400" />
          <span>控制台密钥</span>
        </div>
        <span class="text-[10px] text-slate-400 font-mono">管理</span>
      </button>

      <div
        class="flex items-center justify-between px-2.5 py-2 bg-white border border-slate-200/80 rounded-lg text-xs"
        :title="
          readiness
            ? `可用账号 ${readiness.available_accounts} · 签名服务${readiness.signer_ok ? '就绪' : '不可用'}`
            : '检查服务与模型调用条件'
        "
      >
        <div class="flex items-center gap-2 min-w-0">
          <span
            class="w-2 h-2 rounded-full shrink-0 transition-colors"
            :class="{
              'bg-emerald-500 animate-pulse': healthState === 'online',
              'bg-rose-500': healthState === 'offline',
              'bg-amber-400 animate-pulse': healthState === 'checking',
              'bg-amber-500': healthState === 'degraded',
            }"
          />
          <span class="font-medium text-slate-700 truncate">
            {{
              healthState === 'online'
                ? '模型服务就绪'
                : healthState === 'degraded'
                  ? '在线 · 尚未就绪'
                  : healthState === 'offline'
                    ? '网关服务离线'
                    : '健康探测中…'
            }}
          </span>
        </div>

        <button
          v-if="authStore.authEnabled"
          type="button"
          class="text-slate-400 hover:text-rose-600 transition-colors p-1 rounded hover:bg-rose-50 cursor-pointer shrink-0"
          title="退出登录"
          @click="handleLogout"
        >
          <LogOut class="w-3.5 h-3.5" />
        </button>
      </div>
    </div>
  </aside>
</template>
