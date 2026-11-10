<script setup lang="ts">
import { ref, computed } from 'vue'
import type { ModelStatItem } from '@/types/stats.ts'
import Badge from '@/components/ui/Badge.vue'
import { formatDate } from '@/utils/format.ts'
import {
  Layers,
  ChevronDown,
  ChevronRight,
  Brain,
  Database,
  Users,
  Search,
} from 'lucide-vue-next'

const props = defineProps<{
  stats: Record<string, ModelStatItem>
}>()

const expandedModels = ref<Record<string, boolean>>({})
const searchQuery = ref('')

function toggleExpand(key: string) {
  expandedModels.value[key] = !expandedModels.value[key]
}

const statList = computed(() => {
  const kw = searchQuery.value.trim().toLowerCase()
  return Object.entries(props.stats || {})
    .map(([rawName, item]) => ({
      name: rawName.replace(/^models\//, ''),
      rawName,
      ...item,
    }))
    .filter((item) => !kw || item.name.toLowerCase().includes(kw))
})
</script>

<template>
  <div class="bg-white border border-slate-200/90 rounded-xl shadow-2xs overflow-hidden">
    <!-- Table Header Toolbar -->
    <div class="px-5 py-3.5 border-b border-slate-100 flex flex-col sm:flex-row sm:items-center justify-between gap-3">
      <div class="flex items-center gap-2">
        <Layers class="w-4 h-4 text-brand-600" />
        <h3 class="font-semibold text-slate-900 text-sm">
          模型路由与配额明细
        </h3>
        <span class="px-2 py-0.5 text-[11px] font-mono tabular-nums bg-slate-100 text-slate-600 rounded-md">
          {{ statList.length }} 个活跃模型
        </span>
      </div>

      <div class="relative w-full sm:w-56">
        <Search class="w-3.5 h-3.5 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
        <input
          v-model="searchQuery"
          type="text"
          placeholder="筛选模型标识…"
          class="w-full pl-8 pr-3 py-1.5 bg-slate-50 border border-slate-200 rounded-lg text-xs font-mono outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
        >
      </div>
    </div>

    <div class="overflow-x-auto">
      <table class="w-full text-left border-collapse text-xs">
        <thead>
          <tr class="bg-slate-50/80 border-b border-slate-200/70 text-[11px] font-semibold text-slate-500 uppercase tracking-wider">
            <th class="py-2.5 px-3 w-8" />
            <th class="py-2.5 px-3">
              模型标识
            </th>
            <th class="py-2.5 px-3 text-right">
              请求总数
            </th>
            <th class="py-2.5 px-3 text-right">
              成功
            </th>
            <th class="py-2.5 px-3 text-right">
              429 限流
            </th>
            <th class="py-2.5 px-3 text-right">
              错误
            </th>
            <th class="py-2.5 px-4 text-right">
              总 Tokens
            </th>
            <th class="py-2.5 px-5 text-right">
              最后调用
            </th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100 font-mono text-xs tabular-nums">
          <template
            v-for="item in statList"
            :key="item.rawName"
          >
            <tr
              class="hover:bg-slate-50/80 transition-colors cursor-pointer"
              :class="{ 'bg-slate-50/60': expandedModels[item.rawName] }"
              @click="toggleExpand(item.rawName)"
            >
              <td class="py-3 px-3 text-center text-slate-400">
                <ChevronDown
                  v-if="expandedModels[item.rawName]"
                  class="w-3.5 h-3.5 inline"
                />
                <ChevronRight
                  v-else
                  class="w-3.5 h-3.5 inline"
                />
              </td>
              <td class="py-3 px-3 font-semibold text-slate-900">
                <div class="flex items-center gap-2 flex-wrap">
                  <span>{{ item.name }}</span>
                  <span
                    v-if="item.account_breakdown && item.account_breakdown.length"
                    class="px-1.5 py-0.5 rounded text-[10px] bg-slate-100 text-slate-600 font-sans font-medium"
                  >
                    {{ item.account_breakdown.length }} 节点
                  </span>
                  <span
                    v-if="(item.reasoning_tokens || 0) > 0"
                    class="text-brand-700 bg-brand-50 border border-brand-200/60 px-1.5 py-0.5 rounded text-[10px] font-normal inline-flex items-center gap-1"
                  >
                    <Brain class="w-2.5 h-2.5" />
                    <span>{{ item.reasoning_tokens?.toLocaleString() }}</span>
                  </span>
                  <span
                    v-if="(item.cached_tokens || 0) > 0"
                    class="text-sky-700 bg-sky-50 border border-sky-200/60 px-1.5 py-0.5 rounded text-[10px] font-normal inline-flex items-center gap-1"
                  >
                    <Database class="w-2.5 h-2.5" />
                    <span>{{ item.cached_tokens?.toLocaleString() }}</span>
                  </span>
                </div>
              </td>
              <td class="py-3 px-3 text-right font-semibold text-slate-800">
                {{ (item.requests || 0).toLocaleString() }}
              </td>
              <td class="py-3 px-3 text-right">
                <Badge
                  variant="green"
                  size="sm"
                >
                  {{ (item.success || 0).toLocaleString() }}
                </Badge>
              </td>
              <td class="py-3 px-3 text-right">
                <Badge
                  :variant="(item.rate_limited || 0) > 0 ? 'amber' : 'gray'"
                  size="sm"
                >
                  {{ (item.rate_limited || 0).toLocaleString() }}
                </Badge>
              </td>
              <td class="py-3 px-3 text-right">
                <span
                  :class="(item.errors || 0) > 0 ? 'text-rose-600 font-bold' : 'text-slate-400'"
                >
                  {{ (item.errors || 0).toLocaleString() }}
                </span>
              </td>
              <td class="py-3 px-4 text-right text-slate-600">
                <span :title="`Prompt: ${(item.prompt_tokens || 0).toLocaleString()} / Completion: ${(item.completion_tokens || 0).toLocaleString()}`">
                  {{ (item.total_tokens || 0).toLocaleString() }}
                </span>
              </td>
              <td class="py-3 px-5 text-right text-slate-500">
                {{ formatDate(item.last_used, { withSeconds: true }) }}
              </td>
            </tr>

            <!-- Accordion Sub-Drawer: Account Breakdown for this Model -->
            <tr
              v-if="expandedModels[item.rawName]"
              class="bg-slate-50/70 border-b border-slate-100"
            >
              <td
                colspan="8"
                class="p-3.5"
              >
                <div class="bg-white rounded-lg border border-slate-200/90 p-3.5 shadow-2xs space-y-2.5">
                  <div class="flex items-center justify-between text-xs font-semibold text-slate-700 font-sans">
                    <span class="flex items-center gap-1.5">
                      <Users class="w-3.5 h-3.5 text-brand-600" />
                      <span>节点今日配额分摊明细</span>
                    </span>
                  </div>

                  <div
                    v-if="item.account_breakdown && item.account_breakdown.length"
                    class="overflow-x-auto"
                  >
                    <table class="w-full text-left text-xs border-collapse tabular-nums">
                      <thead>
                        <tr class="text-slate-400 border-b border-slate-100 text-[11px] font-sans">
                          <th class="py-2 px-2">
                            子账号 / 邮箱
                          </th>
                          <th class="py-2 px-2 text-center">
                            会话索引
                          </th>
                          <th class="py-2 px-2 text-center">
                            分组
                          </th>
                          <th class="py-2 px-2 text-center">
                            等级
                          </th>
                          <th class="py-2 px-2 text-right">
                            今日请求
                          </th>
                          <th class="py-2 px-2 text-right">
                            成功 / 429
                          </th>
                          <th class="py-2 px-3 text-center">
                            配额水位
                          </th>
                          <th class="py-2 px-2 text-right">
                            节点状态
                          </th>
                        </tr>
                      </thead>
                      <tbody class="divide-y divide-slate-50">
                        <tr
                          v-for="acc in item.account_breakdown"
                          :key="acc.account_id"
                          class="hover:bg-slate-50/60"
                        >
                          <td class="py-2 px-2">
                            <div class="font-medium text-slate-900 truncate max-w-[200px]">
                              {{ acc.email || acc.name || acc.account_id }}
                            </div>
                            <div class="text-[10px] text-slate-400 truncate">
                              {{ acc.account_id }}
                            </div>
                          </td>
                          <td class="py-2 px-2 text-center font-semibold text-slate-700">
                            u/{{ acc.auth_user }}
                          </td>
                          <td class="py-2 px-2 text-center">
                            <span class="px-1.5 py-0.5 rounded text-[10px] bg-slate-100 text-slate-600">
                              {{ acc.group }}
                            </span>
                          </td>
                          <td class="py-2 px-2 text-center">
                            <span
                              class="px-1.5 py-0.5 rounded text-[10px] font-semibold"
                              :class="[
                                acc.tier === 'Ultra' || acc.tier === 'Ultra_20X'
                                  ? 'bg-sky-50 text-sky-700 border border-sky-200/70'
                                  : acc.tier === 'Pro'
                                    ? 'bg-amber-50 text-amber-700 border border-amber-200/70'
                                    : acc.tier === 'Plus'
                                      ? 'bg-emerald-50 text-emerald-700 border border-emerald-200/70'
                                      : 'bg-slate-100 text-slate-600',
                              ]"
                            >
                              {{ acc.tier }}
                            </span>
                          </td>
                          <td class="py-2 px-2 text-right font-bold text-slate-900">
                            {{ acc.today_requests }}
                          </td>
                          <td class="py-2 px-2 text-right">
                            <span class="text-emerald-600 font-semibold">{{ acc.today_success }}</span>
                            <span class="text-slate-300 mx-1">/</span>
                            <span :class="acc.today_rate_limited > 0 ? 'text-rose-600 font-bold' : 'text-slate-400'">
                              {{ acc.today_rate_limited }}
                            </span>
                          </td>
                          <td class="py-2 px-3 text-center">
                            <div class="w-28 mx-auto space-y-1">
                              <div class="flex justify-between text-[10px] text-slate-500">
                                <span>{{ acc.today_requests }}/{{ acc.configured_quota }}</span>
                                <span>{{ Math.round(acc.quota_used_percent) }}%</span>
                              </div>
                              <div class="w-full bg-slate-100 rounded-full h-1.5 overflow-hidden">
                                <div
                                  class="h-1.5 rounded-full"
                                  :class="acc.quota_used_percent >= 100 ? 'bg-amber-500' : acc.quota_used_percent >= 80 ? 'bg-amber-400' : 'bg-brand-500'"
                                  :style="{ width: `${Math.min(100, Math.round(acc.quota_used_percent))}%` }"
                                />
                              </div>
                            </div>
                          </td>
                          <td class="py-2 px-2 text-right font-sans">
                            <span
                              v-if="!acc.enabled"
                              class="text-[11px] text-slate-400"
                            >
                              已停用
                            </span>
                            <span
                              v-else-if="acc.cooldown_remaining > 0"
                              class="px-1.5 py-0.5 rounded text-[10px] font-mono bg-rose-50 text-rose-700 border border-rose-200/70 font-medium"
                            >
                              429 ({{ acc.cooldown_remaining }}s)
                            </span>
                            <span
                              v-else-if="acc.is_available"
                              class="px-1.5 py-0.5 rounded text-[10px] bg-emerald-50 text-emerald-700 border border-emerald-200/70 font-medium"
                            >
                              就绪
                            </span>
                            <span
                              v-else
                              class="px-1.5 py-0.5 rounded text-[10px] bg-amber-50 text-amber-700 border border-amber-200/70 font-medium"
                            >
                              配额耗尽
                            </span>
                          </td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                  <div
                    v-else
                    class="text-xs text-slate-400 py-3 text-center font-sans"
                  >
                    今日尚无账号调用此模型
                  </div>
                </div>
              </td>
            </tr>
          </template>

          <tr v-if="!statList.length">
            <td
              colspan="8"
              class="py-10 text-center text-slate-400 text-xs font-sans"
            >
              {{ searchQuery ? '未找到匹配的模型统计记录' : '暂无模型调用记录，发起 API 请求后将在此展示实时吞吐' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
