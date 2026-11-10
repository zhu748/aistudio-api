<script setup lang="ts">
import { computed } from 'vue'
import { useAccountsStore } from '@/stores/accounts.ts'
import { useSystemStore } from '@/stores/system.ts'
import { usePolling } from '@/composables/usePolling.ts'
import StatCard from '@/components/ui/StatCard.vue'
import ModelStatsTable from '@/components/dashboard/ModelStatsTable.vue'
import QuickApiExamples from '@/components/dashboard/QuickApiExamples.vue'
import {
  UserCheck,
  Users,
  Activity,
  Brain,
  Database,
  ArrowRightLeft,
  RotateCcw,
  Cpu,
} from 'lucide-vue-next'

const accountsStore = useAccountsStore()
const systemStore = useSystemStore()

const activeAccountDisplay = computed(() => {
  const acc = accountsStore.activeAccount
  if (!acc) return '无激活账号'
  return acc.email || acc.name || acc.id
})

const activeAccountMeta = computed(() => {
  const acc = accountsStore.activeAccount
  if (!acc) return null
  const u = acc.auth_user != null && acc.auth_user !== '' ? `u/${acc.auth_user}` : 'u/0'
  return {
    u,
    tier: acc.tier || 'Free',
    group: acc.group || 'default',
  }
})

const poolSummary = computed(() => {
  const rows = accountsStore.accountRows
  const total = rows.length
  let ready = 0
  let limited = 0
  let disabledOrExpired = 0

  for (const a of rows) {
    if (a.enabled === false || a.session_expired) {
      disabledOrExpired += 1
    } else if (a.is_available === false) {
      limited += 1
    } else {
      ready += 1
    }
  }
  return { total, ready, limited, disabledOrExpired }
})

const successRateDisplay = computed(() => {
  const total = systemStore.totalRequests
  if (!total) return '100.0%'
  return `${((systemStore.totalSuccess / total) * 100).toFixed(1)}%`
})

const reasoningTokens = computed(() => systemStore.stats?.totals?.reasoning_tokens || 0)
const cachedTokens = computed(() => systemStore.stats?.totals?.cached_tokens || 0)

async function handleQuickSwitch() {
  await systemStore.forceNextAccount()
  await accountsStore.fetchAll()
}

async function handleQuickResetCooldown() {
  await systemStore.clearCooldown()
  await accountsStore.fetchAll()
}

async function loadData() {
  await Promise.all([
    accountsStore.fetchAll(),
    systemStore.fetchStats(),
  ])
}

usePolling(loadData, 10000)
</script>

<template>
  <div class="space-y-5">
    <!-- Primary Telemetry Strip (4-Column Engineering Readout) -->
    <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3.5">
      <!-- 1. Active Routing Node -->
      <StatCard
        label="当前调度节点"
        :value="activeAccountDisplay"
        :color="accountsStore.activeAccount ? 'default' : 'amber'"
        :mono="true"
      >
        <template #action>
          <button
            v-if="poolSummary.total > 1"
            type="button"
            :disabled="systemStore.switchingNext"
            class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium text-slate-600 hover:text-slate-900 bg-slate-100 hover:bg-slate-200/80 transition-colors cursor-pointer disabled:opacity-50"
            title="立即顺延切换至下一个可用账号"
            @click="handleQuickSwitch"
          >
            <ArrowRightLeft class="w-2.5 h-2.5" />
            <span>切换</span>
          </button>
        </template>
        <template #icon>
          <UserCheck class="w-4 h-4 text-emerald-600" />
        </template>
        <template #footer>
          <div
            v-if="activeAccountMeta"
            class="flex items-center gap-1.5 pt-1 text-[11px] font-mono"
          >
            <span class="px-1.5 py-0.5 rounded bg-brand-50 text-brand-700 border border-brand-200/70 font-semibold">
              {{ activeAccountMeta.u }}
            </span>
            <span class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-700 border border-slate-200/70 font-medium">
              {{ activeAccountMeta.tier }}
            </span>
            <span class="text-slate-400 truncate">
              分组: {{ activeAccountMeta.group }}
            </span>
          </div>
          <div
            v-else
            class="pt-1 text-[11px] text-slate-400"
          >
            请前往账号凭据池导入或启用账号
          </div>
        </template>
      </StatCard>

      <!-- 2. Account Pool Health -->
      <StatCard
        label="凭据池健康水位"
        :value="`${poolSummary.ready} / ${poolSummary.total}`"
        sub-value="可用就绪"
        color="default"
      >
        <template #icon>
          <Users class="w-4 h-4 text-brand-600" />
        </template>
        <template #footer>
          <div class="flex items-center gap-2 pt-1 text-[11px] font-mono tabular-nums">
            <span class="inline-flex items-center gap-1 text-emerald-700">
              <span class="w-1.5 h-1.5 rounded-full bg-emerald-500" />
              就绪 {{ poolSummary.ready }}
            </span>
            <span class="text-slate-300">·</span>
            <span
              class="inline-flex items-center gap-1"
              :class="poolSummary.limited > 0 ? 'text-amber-700 font-semibold' : 'text-slate-400'"
            >
              <span class="w-1.5 h-1.5 rounded-full bg-amber-500" />
              受限 {{ poolSummary.limited }}
            </span>
            <span class="text-slate-300">·</span>
            <span class="inline-flex items-center gap-1 text-slate-400">
              <span class="w-1.5 h-1.5 rounded-full bg-slate-300" />
              停用 {{ poolSummary.disabledOrExpired }}
            </span>
          </div>
        </template>
      </StatCard>

      <!-- 3. Request Throughput & 429 SLA -->
      <StatCard
        label="请求吞吐与限流"
        :value="systemStore.totalRequests.toLocaleString()"
        :sub-value="`成功率 ${successRateDisplay}`"
        color="default"
      >
        <template #action>
          <button
            v-if="systemStore.totalRateLimited > 0"
            type="button"
            :disabled="systemStore.resettingCooldown"
            class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-medium text-amber-700 bg-amber-50 hover:bg-amber-100 border border-amber-200/70 transition-colors cursor-pointer disabled:opacity-50"
            title="清除所有账号的 429 冷却锁定"
            @click="handleQuickResetCooldown"
          >
            <RotateCcw class="w-2.5 h-2.5" />
            <span>解锁</span>
          </button>
        </template>
        <template #icon>
          <Activity class="w-4 h-4 text-sky-600" />
        </template>
        <template #footer>
          <div class="flex items-center gap-2 pt-1 text-[11px] font-mono tabular-nums">
            <span class="text-emerald-700">
              成功 {{ systemStore.totalSuccess.toLocaleString() }}
            </span>
            <span class="text-slate-300">·</span>
            <span :class="systemStore.totalRateLimited > 0 ? 'text-amber-600 font-semibold' : 'text-slate-400'">
              429限流 {{ systemStore.totalRateLimited.toLocaleString() }}
            </span>
            <span class="text-slate-300">·</span>
            <span :class="systemStore.totalErrors > 0 ? 'text-rose-600 font-semibold' : 'text-slate-400'">
              异常 {{ systemStore.totalErrors.toLocaleString() }}
            </span>
          </div>
        </template>
      </StatCard>

      <!-- 4. Token Telemetry -->
      <StatCard
        label="累计 Token 消耗"
        :value="systemStore.totalTokens.toLocaleString()"
        :sub-value="`入 ${systemStore.promptTokens.toLocaleString()} / 出 ${systemStore.completionTokens.toLocaleString()}`"
        color="default"
      >
        <template #icon>
          <Cpu class="w-4 h-4 text-brand-600" />
        </template>
        <template #footer>
          <div class="flex items-center gap-2 pt-1 text-[11px] font-mono tabular-nums">
            <span class="inline-flex items-center gap-1 text-slate-600 bg-slate-100 px-1.5 py-0.5 rounded">
              <Brain class="w-3 h-3 text-brand-600" />
              思考 {{ reasoningTokens.toLocaleString() }}
            </span>
            <span class="inline-flex items-center gap-1 text-slate-600 bg-slate-100 px-1.5 py-0.5 rounded">
              <Database class="w-3 h-3 text-sky-600" />
              缓存 {{ cachedTokens.toLocaleString() }}
            </span>
          </div>
        </template>
      </StatCard>
    </div>

    <!-- Model Statistics Table with Account Breakdown Accordion -->
    <ModelStatsTable
      :stats="systemStore.stats?.models || {}"
    />

    <!-- Multi-Protocol Quick API Reference Snippets -->
    <QuickApiExamples />
  </div>
</template>
