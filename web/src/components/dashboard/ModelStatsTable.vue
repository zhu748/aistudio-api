<script setup lang="ts">
import { computed, ref } from 'vue'
import type { ModelStatItem } from '@/types/stats.ts'
import { Search, ArrowUpRight, ChevronDown } from 'lucide-vue-next'

const props = defineProps<{ stats: Record<string, ModelStatItem>; since?: number; until?: number }>()
const search = ref('')
const expanded = ref('')
const sort = ref('requests')
const rows = computed(() =>
  Object.entries(props.stats)
    .map(([name, item]) => ({ name, ...item }))
    .filter((row) => row.name.toLowerCase().includes(search.value.trim().toLowerCase()))
    .sort((a, b) =>
      sort.value === 'latency'
        ? (b.latency_p95_ms || 0) - (a.latency_p95_ms || 0)
        : sort.value === 'errors'
          ? b.requests - b.success - (a.requests - a.success)
          : b.requests - a.requests,
    ),
)
function seconds(value?: number | null) {
  return value == null ? '—' : `${(value / 1000).toFixed(2)}s`
}
function requestLink(model: string, failed = false) {
  return {
    path: '/requests',
    query: {
      model: model === '未识别模型' ? '__unknown__' : model,
      ...(props.since ? { since: String(props.since) } : {}),
      ...(props.until ? { until: String(props.until) } : {}),
      ...(failed ? { result: 'failed' } : {}),
    },
  }
}
</script>

<template>
  <section class="surface-card rounded-xl overflow-hidden">
    <div class="px-5 py-4 border-b border-slate-100 flex flex-wrap items-center justify-between gap-3">
      <div>
        <h2 class="font-semibold text-sm">
          模型调用统计 <span class="text-slate-400 font-normal ml-2">{{ rows.length }} 个模型</span>
        </h2>
        <p class="text-xs text-slate-500 mt-1">
          按最终调用结果统计；失败后重试成功仍计为一次成功。
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <div class="relative">
          <Search class="w-4 h-4 absolute left-2.5 top-2 text-slate-400" /><input
            v-model="search"
            aria-label="搜索模型"
            class="field-input pl-8 w-48"
            placeholder="搜索模型"
          >
        </div>
        <select
          v-model="sort"
          aria-label="统计排序"
          class="field-input"
        >
          <option value="requests">
            调用数排序
          </option>
          <option value="errors">
            未成功数排序
          </option>
          <option value="latency">
            P95 耗时排序
          </option>
        </select>
      </div>
    </div>
    <div class="overflow-x-auto">
      <table class="w-full text-sm text-left whitespace-nowrap">
        <thead class="bg-slate-50 text-xs text-slate-500">
          <tr>
            <th class="p-4">
              模型
            </th>
            <th class="p-3 text-right">
              调用 / 成功率
            </th>
            <th class="p-3 text-right">
              未成功
            </th>
            <th class="p-3 text-right">
              上游尝试 / 重试率
            </th>
            <th class="p-3 text-right">
              首包 P50
            </th>
            <th class="p-3 text-right">
              耗时 P95
            </th>
            <th class="p-4 text-right">
              Token
            </th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          <template
            v-for="row in rows"
            :key="row.name"
          >
            <tr class="hover:bg-slate-50/70">
              <td class="p-4">
                <button
                  class="inline-flex items-center gap-2 font-medium text-slate-900 cursor-pointer"
                  :aria-expanded="expanded === row.name"
                  @click="expanded = expanded === row.name ? '' : row.name"
                >
                  <ChevronDown
                    class="w-4 h-4 text-slate-400 transition-transform"
                    :class="expanded === row.name ? '' : '-rotate-90'"
                  /><span class="font-mono text-xs">{{ row.name }}</span>
                </button>
              </td>
              <td class="p-3 text-right tabular-nums">
                <router-link
                  :to="requestLink(row.name)"
                  class="text-brand-700 font-semibold hover:underline"
                >
                  {{ row.requests.toLocaleString() }}
                </router-link>
                <div class="text-xs text-slate-500 mt-1">
                  {{ row.success_rate == null ? '—' : `${row.success_rate.toFixed(1)}%` }}
                </div>
              </td>
              <td class="p-3 text-right">
                <router-link
                  :to="requestLink(row.name, true)"
                  :class="
                    row.requests > row.success
                      ? 'text-rose-600 hover:underline font-semibold'
                      : 'text-slate-400'
                  "
                >
                  {{ row.requests - row.success }}
                </router-link>
              </td>
              <td class="p-3 text-right tabular-nums">
                {{ row.attempts || 0 }}
                <div class="text-xs text-slate-500 mt-1">
                  {{ row.retry_rate == null ? '—' : `${row.retry_rate.toFixed(1)}%` }}
                </div>
              </td>
              <td class="p-3 text-right tabular-nums">
                {{ seconds(row.first_byte_p50_ms) }}
              </td>
              <td class="p-3 text-right tabular-nums">
                {{ seconds(row.latency_p95_ms) }}
              </td>
              <td class="p-4 text-right tabular-nums">
                {{ row.total_tokens.toLocaleString() }}
              </td>
            </tr>
            <tr v-if="expanded === row.name">
              <td
                colspan="7"
                class="bg-slate-50 px-6 py-4"
              >
                <div class="flex flex-wrap gap-x-6 gap-y-3 text-xs text-slate-600">
                  <span>输入 {{ row.prompt_tokens.toLocaleString() }}</span><span>输出 {{ row.completion_tokens.toLocaleString() }}</span><span>思考 {{ (row.reasoning_tokens || 0).toLocaleString() }}</span><span>缓存 {{ (row.cached_tokens || 0).toLocaleString() }}</span><span>总耗时 P50 {{ seconds(row.latency_p50_ms) }}</span><span>首包 P95 {{ seconds(row.first_byte_p95_ms) }}</span><router-link
                    :to="requestLink(row.name)"
                    class="inline-flex items-center gap-1 text-brand-700"
                  >
                    查看调用记录<ArrowUpRight class="w-3.5 h-3.5" />
                  </router-link>
                </div>
                <div class="flex flex-wrap gap-2 mt-3">
                  <span
                    v-for="(count, reason) in row.finish_reasons"
                    :key="reason"
                    class="px-2 py-1 rounded bg-white border border-slate-200 text-xs"
                  >{{ reason }} · {{ count }}</span>
                </div>
                <div
                  v-if="row.accounts?.length"
                  class="mt-4 overflow-x-auto rounded-lg border border-slate-200 bg-white"
                >
                  <table class="w-full text-xs">
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
                          耗时 P95
                        </th>
                        <th class="p-3 text-right">
                          Token
                        </th>
                      </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-100">
                      <tr
                        v-for="account in row.accounts"
                        :key="account.account_id"
                      >
                        <td class="p-3">
                          <router-link
                            :to="{
                              path: '/requests',
                              query: {
                                ...requestLink(row.name).query,
                                account_id:
                                  account.account_id === '未调度' ? '__unassigned__' : account.account_id,
                              },
                            }"
                            class="text-brand-700 hover:underline"
                          >
                            {{ account.name }}
                          </router-link>
                        </td>
                        <td class="p-3 text-right">
                          {{ account.requests }}
                        </td>
                        <td class="p-3 text-right">
                          {{ account.success_rate == null ? '—' : `${account.success_rate.toFixed(1)}%` }}
                        </td>
                        <td class="p-3 text-right">
                          {{ seconds(account.latency_p95_ms) }}
                        </td>
                        <td class="p-3 text-right">
                          {{ account.total_tokens.toLocaleString() }}
                        </td>
                      </tr>
                    </tbody>
                  </table>
                </div>
              </td>
            </tr>
          </template>
          <tr v-if="!rows.length">
            <td
              colspan="7"
              class="p-10 text-center text-slate-400"
            >
              {{ search ? '没有匹配的模型' : '当前时间范围暂无模型调用' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>
