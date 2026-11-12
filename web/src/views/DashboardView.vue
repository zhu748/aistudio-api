<script setup lang="ts">
import { computed, watch } from 'vue'
import { useAccountsStore } from '@/stores/accounts.ts'
import { useSystemStore } from '@/stores/system.ts'
import { usePolling } from '@/composables/usePolling.ts'
import StatCard from '@/components/ui/StatCard.vue'
import ModelStatsTable from '@/components/dashboard/ModelStatsTable.vue'
import QuickApiExamples from '@/components/dashboard/QuickApiExamples.vue'
import RequestTrend from '@/components/dashboard/RequestTrend.vue'
import { usePageRefresh } from '@/composables/usePageRefresh.ts'
import { UserCheck, Users, Activity, Brain, Database, ArrowRightLeft, RotateCcw, Cpu } from 'lucide-vue-next'

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
    } else if (a.availability === 'partial' || a.is_available === false) {
      limited += 1
    } else {
      ready += 1
    }
  }
  return { total, ready, limited, disabledOrExpired }
})

const successRateDisplay = computed(() => {
  const total = systemStore.totalRequests
  if (!total) return '—'
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
  const results = await Promise.all([accountsStore.fetchAll(), systemStore.fetchStats()])
  return results.every(Boolean)
}

usePolling(loadData, 10000)
usePageRefresh(loadData)
watch(
  () => systemStore.statsRange,
  (value) => {
    if (value !== 'custom') void systemStore.fetchStats()
  },
)
function seconds(value?: number | null) {
  return value == null ? '—' : `${(value / 1000).toFixed(2)}s`
}
const rangeQuery = computed(() => ({
  since: String(systemStore.stats?.since || ''),
  until: String(systemStore.stats?.until || ''),
}))
</script>

<template>
  <div class="space-y-5">
    <div class="surface-card rounded-xl p-4 flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="text-sm font-semibold">
          调用概览
        </h2>
        <p class="text-xs text-slate-500 mt-1">
          每次客户端调用按最终结果计一次；账号健康状态为当前值。
        </p>
      </div>
      <div class="flex flex-wrap gap-2 items-center">
        <select
          v-model="systemStore.statsRange"
          aria-label="统计时间范围"
          class="field-input"
        >
          <option value="today">
            今日（本地时间）
          </option>
          <option value="24h">
            最近 24 小时
          </option>
          <option value="7d">
            最近 7 天
          </option>
          <option value="14d">
            最近 14 天
          </option>
          <option value="custom">
            自定义时间
          </option>
        </select>
        <template v-if="systemStore.statsRange === 'custom'">
          <input
            v-model="systemStore.customSince"
            aria-label="统计开始时间"
            type="datetime-local"
            class="field-input"
          ><input
            v-model="systemStore.customUntil"
            aria-label="统计结束时间"
            type="datetime-local"
            class="field-input"
          ><button
            class="px-3 py-2 bg-slate-900 text-white rounded-lg text-xs"
            @click="systemStore.fetchStats()"
          >
            查询
          </button>
        </template>
      </div>
    </div>
    <p
      v-if="systemStore.statsError"
      role="alert"
      class="p-3 rounded-lg bg-rose-50 text-rose-700 text-sm"
    >
      {{ systemStore.statsError }} · 当前展示可能为上次成功获取的数据。
    </p>
    <p
      v-if="systemStore.stats && !systemStore.stats.history_enabled"
      class="p-3 rounded-lg bg-amber-50 text-amber-800 text-sm"
    >
      请求历史采集已关闭，当前仅展示已保留记录。
    </p>
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
            <span
              class="px-1.5 py-0.5 rounded bg-brand-50 text-brand-700 border border-brand-200/70 font-semibold"
            >
              {{ activeAccountMeta.u }}
            </span>
            <span
              class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-700 border border-slate-200/70 font-medium"
            >
              {{ activeAccountMeta.tier }}
            </span>
            <span class="text-slate-400 truncate"> 分组: {{ activeAccountMeta.group }} </span>
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
        sub-value="凭据就绪且无已知冷却"
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
              模型冷却 {{ poolSummary.limited }}
            </span>
            <span class="text-slate-300">·</span>
            <span class="inline-flex items-center gap-1 text-slate-400">
              <span class="w-1.5 h-1.5 rounded-full bg-slate-300" />
              停用/失效 {{ poolSummary.disabledOrExpired }}
            </span>
          </div>
        </template>
      </StatCard>

      <!-- 3. Request Throughput & 429 SLA -->
      <StatCard
        label="所选时段调用数"
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
            <span class="text-emerald-700"> 成功 {{ systemStore.totalSuccess.toLocaleString() }} </span>
            <span class="text-slate-300">·</span>
            <span
              :class="systemStore.totalRateLimited > 0 ? 'text-amber-600 font-semibold' : 'text-slate-400'"
            >
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
        label="所选时段 Token"
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

    <div class="grid grid-cols-2 xl:grid-cols-4 gap-3">
      <StatCard
        label="正在执行"
        :value="systemStore.stats?.active || 0"
      />
      <StatCard
        label="首包 P50"
        :value="seconds(systemStore.stats?.totals.first_byte_p50_ms)"
      />
      <StatCard
        label="总耗时 P95"
        :value="seconds(systemStore.stats?.totals.latency_p95_ms)"
      />
      <StatCard
        label="重试调用占比"
        :value="
          systemStore.stats?.totals.retry_rate == null
            ? '—'
            : `${systemStore.stats.totals.retry_rate.toFixed(1)}%`
        "
        :sub-value="`上游尝试 ${systemStore.stats?.totals.attempts || 0} 次`"
      />
    </div>
    <RequestTrend
      v-if="systemStore.stats"
      :stats="systemStore.stats"
    />

    <!-- Model Statistics Table with Account Breakdown Accordion -->
    <ModelStatsTable
      :stats="systemStore.stats?.models || {}"
      :since="systemStore.stats?.since"
      :until="systemStore.stats?.until"
    />

    <div class="grid grid-cols-1 xl:grid-cols-2 gap-5">
      <section class="surface-card rounded-xl overflow-hidden">
        <h2 class="p-4 border-b border-slate-100 text-sm font-semibold">
          账号调用分布
        </h2>
        <div class="overflow-x-auto">
          <table class="w-full text-xs text-left">
            <thead class="bg-slate-50 text-slate-500">
              <tr>
                <th class="p-3">
                  最终服务账号
                </th>
                <th class="p-3 text-right">
                  调用
                </th>
                <th class="p-3 text-right">
                  成功率
                </th>
                <th class="p-3 text-right">
                  Token
                </th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              <tr
                v-for="(item, id) in systemStore.stats?.accounts"
                :key="id"
              >
                <td class="p-3 max-w-56 truncate">
                  <router-link
                    :to="{
                      path: '/requests',
                      query: {
                        ...rangeQuery,
                        account_id: String(id) === '未调度' ? '__unassigned__' : String(id),
                      },
                    }"
                    class="text-brand-700 hover:underline"
                    :title="item.name"
                  >
                    {{ item.name }}
                  </router-link>
                </td>
                <td class="p-3 text-right tabular-nums">
                  {{ item.requests }}
                </td>
                <td class="p-3 text-right tabular-nums">
                  {{ item.success_rate == null ? '—' : `${item.success_rate.toFixed(1)}%` }}
                </td>
                <td class="p-3 text-right tabular-nums">
                  {{ item.total_tokens.toLocaleString() }}
                </td>
              </tr>
              <tr v-if="!Object.keys(systemStore.stats?.accounts || {}).length">
                <td
                  colspan="4"
                  class="p-8 text-center text-slate-400"
                >
                  暂无账号调用
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
      <section class="surface-card rounded-xl overflow-hidden">
        <h2 class="p-4 border-b border-slate-100 text-sm font-semibold">
          客户端密钥分布
        </h2>
        <div class="overflow-x-auto">
          <table class="w-full text-xs text-left">
            <thead class="bg-slate-50 text-slate-500">
              <tr>
                <th class="p-3">
                  密钥标识
                </th>
                <th class="p-3 text-right">
                  调用
                </th>
                <th class="p-3 text-right">
                  成功率
                </th>
                <th class="p-3 text-right">
                  Token
                </th>
              </tr>
            </thead>
            <tbody class="divide-y divide-slate-100">
              <tr
                v-for="(item, id) in systemStore.stats?.keys"
                :key="id"
              >
                <td class="p-3 font-mono">
                  <router-link
                    :to="{
                      path: '/requests',
                      query: { ...rangeQuery, key_id: String(id) === '匿名' ? '__anonymous__' : String(id) },
                    }"
                    class="text-brand-700 hover:underline"
                  >
                    {{ id }}
                  </router-link>
                </td>
                <td class="p-3 text-right">
                  {{ item.requests }}
                </td>
                <td class="p-3 text-right">
                  {{ item.success_rate == null ? '—' : `${item.success_rate.toFixed(1)}%` }}
                </td>
                <td class="p-3 text-right">
                  {{ item.total_tokens.toLocaleString() }}
                </td>
              </tr>
              <tr v-if="!Object.keys(systemStore.stats?.keys || {}).length">
                <td
                  colspan="4"
                  class="p-8 text-center text-slate-400"
                >
                  暂无密钥调用
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>
    <p class="text-xs text-slate-500">
      统计来自保留期内的请求记录（最多
      {{ systemStore.stats?.history_days || 14 }} 天）；清空请求历史会同步清除此范围的统计。
    </p>

    <!-- Multi-Protocol Quick API Reference Snippets -->
    <details class="surface-card rounded-xl p-4">
      <summary class="text-sm font-medium cursor-pointer">
        API 接入示例
      </summary>
      <div class="mt-4">
        <QuickApiExamples />
      </div>
    </details>
  </div>
</template>
