<script setup lang="ts">
import { computed } from 'vue'
import { Menu, Key, RefreshCw, ShieldAlert, ShieldCheck, Radio } from 'lucide-vue-next'
import { useAuthStore } from '@/stores/auth.ts'
import { useAccountsStore } from '@/stores/accounts.ts'

defineProps<{
  title: string
  subtitle?: string
  refreshing?: boolean
}>()

const emit = defineEmits<{
  toggleSidebar: []
  openTokenModal: []
  refresh: []
}>()

const authStore = useAuthStore()
const accountsStore = useAccountsStore()

const activeNodeLabel = computed(() => {
  const acc = accountsStore.activeAccount
  if (!acc) return null
  const label = acc.email || acc.name || acc.id
  const u = acc.auth_user != null && acc.auth_user !== '' ? `u/${acc.auth_user}` : ''
  return { label, u, tier: acc.tier || 'Free' }
})
</script>

<template>
  <header class="h-14 bg-white/85 backdrop-blur-md border-b border-slate-200/90 px-4 sm:px-6 flex items-center justify-between sticky top-0 z-30">
    <div class="flex items-center gap-3 min-w-0">
      <button
        type="button"
        class="lg:hidden p-1.5 rounded-lg text-slate-600 hover:bg-slate-100 transition-colors cursor-pointer"
        @click="emit('toggleSidebar')"
      >
        <Menu class="w-5 h-5" />
      </button>
      <div class="flex items-baseline gap-2.5 min-w-0">
        <h1 class="text-sm sm:text-base font-bold text-slate-900 tracking-tight truncate">
          {{ title }}
        </h1>
        <span
          v-if="subtitle"
          class="hidden md:inline text-xs text-slate-400 truncate"
        >
          {{ subtitle }}
        </span>
      </div>
    </div>

    <div class="flex items-center gap-2">
      <!-- Active Node Telemetry Pill -->
      <router-link
        v-if="activeNodeLabel"
        to="/accounts"
        class="hidden sm:inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-slate-50 hover:bg-slate-100 border border-slate-200/80 text-[11px] font-mono text-slate-700 transition-colors"
        title="当前激活调度节点，点击前往账号池"
      >
        <Radio class="w-3 h-3 text-emerald-500 shrink-0" />
        <span
          v-if="activeNodeLabel.u"
          class="font-bold text-brand-700"
        >{{ activeNodeLabel.u }}</span>
        <span class="truncate max-w-[160px]">{{ activeNodeLabel.label }}</span>
      </router-link>

      <!-- Auth State Badge -->
      <div
        v-if="!authStore.authEnabled"
        class="hidden md:flex items-center gap-1 px-2 py-1 rounded-lg bg-amber-50/80 border border-amber-200/70 text-[11px] font-medium text-amber-800"
        title="服务端未设置 AISTUDIO_WEB_PASSWORD，当前处于免密公开访问模式"
      >
        <ShieldAlert class="w-3 h-3 text-amber-600 shrink-0" />
        <span>免密访问</span>
      </div>
      <div
        v-else
        class="hidden md:flex items-center gap-1 px-2 py-1 rounded-lg bg-emerald-50/80 border border-emerald-200/70 text-[11px] font-medium text-emerald-800"
        title="服务端已启用 AISTUDIO_WEB_PASSWORD 控制台鉴权"
      >
        <ShieldCheck class="w-3 h-3 text-emerald-600 shrink-0" />
        <span>已鉴权</span>
      </div>

      <!-- Refresh Button -->
      <button
        type="button"
        :disabled="refreshing"
        class="inline-flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium text-slate-600 hover:text-slate-900 bg-white hover:bg-slate-50 border border-slate-200/90 rounded-lg transition-all cursor-pointer disabled:opacity-50 shadow-2xs"
        title="立即刷新全局状态与统计数据"
        @click="emit('refresh')"
      >
        <RefreshCw
          class="w-3.5 h-3.5 transition-transform"
          :class="{ 'animate-spin text-brand-600': refreshing }"
        />
        <span class="hidden sm:inline">刷新</span>
      </button>

      <button
        v-if="authStore.authEnabled"
        type="button"
        class="flex items-center gap-1.5 px-2.5 py-1.5 text-xs font-medium text-slate-700 bg-slate-100 hover:bg-slate-200/70 rounded-lg transition-colors cursor-pointer"
        @click="emit('openTokenModal')"
      >
        <Key class="w-3.5 h-3.5 text-slate-500" />
        <span class="hidden sm:inline">密码</span>
      </button>
    </div>
  </header>
</template>
