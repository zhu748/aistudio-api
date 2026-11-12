<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Download, RefreshCw, Search, Trash2, Pause, Play, SlidersHorizontal } from 'lucide-vue-next'
import { operationsApi, type RequestRecord } from '@/api/operations.ts'
import RequestDetail from '@/components/RequestDetail.vue'
import Button from '@/components/ui/Button.vue'
import { usePolling } from '@/composables/usePolling.ts'
import { usePageRefresh } from '@/composables/usePageRefresh.ts'
import { useConfirm } from '@/composables/useConfirm.ts'
import { useToastStore } from '@/stores/toast.ts'
import {
  duration,
  resultClass,
  resultLabels,
  protocolLabels,
  stageLabels,
  downloadJson,
} from '@/utils/requests.ts'

const route = useRoute()
const router = useRouter()
const toast = useToastStore()
const { confirm } = useConfirm()
const rows = ref<RequestRecord[]>([])
const active = ref<RequestRecord[]>([])
const total = ref(0)
const loading = ref(false)
const exporting = ref(false)
const error = ref('')
const activeError = ref('')
const autoRefresh = ref(true)
const advanced = ref(false)
const updatedAt = ref('')
const options = ref<Awaited<ReturnType<typeof operationsApi.options>> | null>(null)
const filters = ref({
  search: '',
  model: '',
  account_id: '',
  key_id: '',
  result: '',
  protocol: '',
  since: '',
  until: '',
  retried: '',
  min_elapsed_ms: '',
})
const filterKeys = Object.keys(filters.value) as (keyof typeof filters.value)[]
const page = computed(() => Math.max(1, Math.floor(Number(route.query.page) || 1)))
const pages = computed(() => Math.max(1, Math.ceil(total.value / 50)))
const selectedId = computed(() => (typeof route.query.request_id === 'string' ? route.query.request_id : ''))
let sequence = 0

function localDate(timestamp: string) {
  const date = new Date(Number(timestamp) * 1000)
  return Number.isFinite(date.getTime())
    ? new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
    : ''
}
function params() {
  const query = new URLSearchParams({ page: String(page.value), page_size: '50' })
  for (const key of filterKeys)
    if (typeof route.query[key] === 'string' && route.query[key]) query.set(key, String(route.query[key]))
  return query
}
async function load() {
  const current = ++sequence
  loading.value = true
  const outcomes = await Promise.allSettled([
    operationsApi
      .requests(params())
      .then((response) => {
        if (current !== sequence) return
        rows.value = response.items
        total.value = response.total
        error.value = ''
        updatedAt.value = new Date().toLocaleTimeString()
        if (page.value > pages.value)
          void router.replace({ query: { ...route.query, page: String(pages.value) } })
      })
      .catch((err) => {
        if (current === sequence) error.value = err instanceof Error ? err.message : '请求历史加载失败'
        throw err
      }),
    operationsApi
      .active()
      .then((response) => {
        if (current === sequence) {
          active.value = response.items
          activeError.value = ''
        }
      })
      .catch((err) => {
        if (current === sequence) activeError.value = err instanceof Error ? err.message : '执行状态加载失败'
        throw err
      }),
  ])
  if (current === sequence) loading.value = false
  return outcomes.every((item) => item.status === 'fulfilled')
}
async function apply() {
  if (
    filters.value.since &&
    filters.value.until &&
    new Date(filters.value.since) > new Date(filters.value.until)
  ) {
    toast.error('开始时间不能晚于结束时间')
    return
  }
  const query: Record<string, string> = {}
  for (const key of filterKeys) {
    const value = filters.value[key].trim()
    if (!value) continue
    if (key === 'since' || key === 'until') query[key] = String(new Date(value).getTime() / 1000)
    else query[key] = value
  }
  const previous = route.fullPath
  await router.replace({ path: '/requests', query })
  if (route.fullPath === previous) await load()
}
async function reset() {
  const previous = route.fullPath
  await router.replace({ path: '/requests', query: {} })
  if (route.fullPath === previous) await load()
}
async function turn(delta: number) {
  await router.replace({ query: { ...route.query, page: String(page.value + delta) } })
}
function showDetail(row: RequestRecord) {
  void router.replace({ query: { ...route.query, request_id: row.request_id } })
}
function closeDetail() {
  const query = { ...route.query }
  delete query.request_id
  void router.replace({ query })
}
async function exportRows() {
  exporting.value = true
  try {
    const response = await operationsApi.exportRequests(params())
    downloadJson(response.items, `requests-${new Date().toISOString().slice(0, 10)}.json`)
    toast.success(`已导出 ${response.items.length} / ${response.total} 条记录，单次上限 10000 条`)
  } catch (err) {
    toast.error(err instanceof Error ? err.message : '导出失败')
  } finally {
    exporting.value = false
  }
}
async function clear() {
  if (!(await confirm('清空全部已完成请求历史和对应统计？执行中的调用会保留。此操作无法恢复。'))) return
  try {
    await operationsApi.clearRequests()
    await load()
    toast.success('已清空已完成请求历史')
  } catch (err) {
    toast.error(err instanceof Error ? err.message : '清空失败')
  }
}
const visibleActive = computed(() =>
  active.value.filter((row) => {
    if (route.query.result) return false
    const model = row.model || row.requested_model || ''
    if (
      route.query.model &&
      model.replace(/^models\//, '') !==
        (route.query.model === '__unknown__' ? '' : String(route.query.model).replace(/^models\//, ''))
    )
      return false
    if (
      route.query.account_id &&
      row.account_id !== (route.query.account_id === '__unassigned__' ? '' : route.query.account_id)
    ) return false
    if (
      route.query.key_id &&
      row.key_id !== (route.query.key_id === '__anonymous__' ? '' : route.query.key_id)
    ) return false
    if (route.query.protocol && row.protocol !== route.query.protocol) return false
    if (route.query.since && row.started_at < Number(route.query.since)) return false
    if (route.query.until && row.started_at > Number(route.query.until)) return false
    if (route.query.retried === 'true' && row.attempts.length < 2) return false
    if (route.query.min_elapsed_ms && (row.elapsed_ms || 0) < Number(route.query.min_elapsed_ms)) return false
    return (
      !route.query.search ||
      `${row.request_id} ${row.model} ${row.account_id} ${row.account_name || ''}`
        .toLowerCase()
        .includes(String(route.query.search).toLowerCase())
    )
  }),
)
watch(
  () => JSON.stringify([...filterKeys, 'page'].map((key) => route.query[key] || '')),
  () => {
    for (const key of filterKeys) {
      const value = typeof route.query[key] === 'string' ? String(route.query[key]) : ''
      filters.value[key] = (key === 'since' || key === 'until') && value ? localDate(value) : value
    }
    if (filters.value.since || filters.value.until || filters.value.protocol || filters.value.key_id)
      advanced.value = true
    void load()
  },
  { immediate: true },
)
onMounted(async () => {
  try {
    options.value = await operationsApi.options()
  } catch {
    toast.error('筛选选项加载失败，可继续输入 ID 查询')
  }
})
usePolling(
  async () => {
    if (autoRefresh.value) await load()
  },
  10000,
  { immediate: false },
)
usePageRefresh(load)
</script>

<template>
  <div class="space-y-4">
    <div class="surface-card rounded-xl p-5 flex flex-wrap justify-between gap-3">
      <div>
        <h2 class="text-base font-semibold">
          请求记录 <span class="text-brand-700 tabular-nums ml-2">{{ total.toLocaleString() }}</span>
        </h2>
        <p class="text-xs text-slate-500 mt-1">
          每次调用保留一条最终记录 · {{ options?.history_days || 14 }} 天保留期<span v-if="updatedAt">
            · 更新于 {{ updatedAt }}</span>
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <Button
          :loading="loading"
          @click="load"
        >
          <RefreshCw class="w-3.5 h-3.5" />刷新
        </Button><Button
          :loading="exporting"
          @click="exportRows"
        >
          <Download class="w-3.5 h-3.5" />导出
        </Button><Button @click="autoRefresh = !autoRefresh">
          <Pause
            v-if="autoRefresh"
            class="w-3.5 h-3.5"
          /><Play
            v-else
            class="w-3.5 h-3.5"
          />{{ autoRefresh ? '自动刷新' : '已暂停' }}
        </Button>
        <details class="relative">
          <summary class="text-sm px-2 cursor-pointer text-slate-500">
            更多
          </summary>
          <div
            class="absolute right-0 top-8 z-10 p-2 rounded-lg bg-white shadow-lg border border-slate-200 whitespace-nowrap"
          >
            <Button
              variant="danger"
              @click="clear"
            >
              <Trash2 class="w-3.5 h-3.5" />清空已完成记录
            </Button>
          </div>
        </details>
      </div>
    </div>
    <p
      v-if="options && !options.enabled"
      class="bg-amber-50 text-amber-800 rounded-lg p-3 text-sm"
    >
      请求历史采集已关闭，新的调用结束后不会保存。执行中状态仍可查看。
    </p>
    <form
      class="surface-card rounded-xl p-4 space-y-4"
      @submit.prevent="apply"
    >
      <div class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3">
        <label class="field-label">关键词<input
          v-model="filters.search"
          class="field-input"
          placeholder="请求 ID、模型、账号名称"
        ></label>
        <label class="field-label">模型<input
          v-model="filters.model"
          list="request-models"
          class="field-input"
          placeholder="全部模型"
        ><datalist id="request-models">
          <option
            v-for="item in options?.models"
            :key="item"
            :value="item"
          /></datalist></label>
        <label class="field-label">账号<input
          v-model="filters.account_id"
          list="request-accounts"
          class="field-input"
          placeholder="选择账号或输入 ID"
        ><datalist id="request-accounts">
          <option
            v-for="item in options?.accounts"
            :key="item.id"
            :value="item.id"
          >
            {{ item.name }}
          </option>
        </datalist></label>
        <label class="field-label">最终结果<select
          v-model="filters.result"
          class="field-input"
        >
          <option value="">全部结果</option>
          <option value="failed">全部未成功</option>
          <option
            v-for="(label, key) in resultLabels"
            v-show="key !== 'running'"
            :key="key"
            :value="key"
          >
            {{ label }}
          </option>
        </select></label>
      </div>
      <div
        v-if="advanced"
        class="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-3 pt-3 border-t border-slate-100"
      >
        <label class="field-label">开始时间<input
          v-model="filters.since"
          type="datetime-local"
          class="field-input"
        ></label><label class="field-label">结束时间<input
          v-model="filters.until"
          type="datetime-local"
          class="field-input"
        ></label><label class="field-label">协议<select
          v-model="filters.protocol"
          class="field-input"
        >
          <option value="">全部协议</option>
          <option
            v-for="(label, key) in protocolLabels"
            :key="key"
            :value="key"
          >
            {{ label }}
          </option>
        </select></label><label class="field-label">密钥<input
          v-model="filters.key_id"
          list="request-keys"
          class="field-input"
          placeholder="选择密钥标识"
        ><datalist id="request-keys">
          <option
            v-for="item in options?.keys"
            :key="item"
            :value="item"
          >
            {{ options?.key_names[item] || item }}
          </option>
        </datalist></label>
      </div>
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="flex flex-wrap gap-4 items-center text-xs text-slate-600">
          <label class="flex gap-1.5 items-center"><input
            v-model="filters.retried"
            type="checkbox"
            true-value="true"
            false-value=""
            class="accent-brand-600"
          >有重试</label><label class="flex gap-1.5 items-center"><input
            v-model="filters.min_elapsed_ms"
            type="checkbox"
            true-value="30000"
            false-value=""
            class="accent-brand-600"
          >慢请求 ≥ 30s</label><button
            type="button"
            class="inline-flex gap-1 items-center text-slate-500 cursor-pointer"
            @click="advanced = !advanced"
          >
            <SlidersHorizontal class="w-3.5 h-3.5" />{{ advanced ? '收起筛选' : '更多筛选' }}
          </button>
        </div>
        <div class="flex gap-2">
          <Button @click="reset">
            重置
          </Button><Button
            type="submit"
            variant="primary"
          >
            <Search class="w-3.5 h-3.5" />查询
          </Button>
        </div>
      </div>
    </form>
    <p
      v-if="error"
      role="alert"
      class="bg-rose-50 text-rose-700 rounded-lg p-3 text-sm"
    >
      {{ error }} · 请重试；现有列表可能是上次获取的数据。
    </p>
    <p
      v-if="activeError"
      role="alert"
      class="text-amber-700 text-xs"
    >
      执行中状态暂不可用：{{ activeError }}
    </p>
    <section
      v-if="visibleActive.length"
      class="rounded-xl border border-sky-200 bg-sky-50/60 p-4 space-y-3"
    >
      <h3 class="font-semibold text-sm text-sky-800">
        正在执行 · {{ visibleActive.length }}
      </h3>
      <button
        v-for="row in visibleActive"
        :key="row.request_id"
        class="w-full text-left rounded-lg bg-white border border-sky-100 p-3 flex flex-wrap items-center justify-between gap-2 cursor-pointer"
        @click="showDetail(row)"
      >
        <span class="text-sm font-medium break-all">{{ row.model || row.requested_model || '正在解析模型'
        }}<span class="text-slate-400 font-normal text-xs ml-2">{{
          row.account_name || row.account_id || '等待调度'
        }}</span></span><span class="text-xs text-sky-700">{{ stageLabels[row.stage || ''] || row.stage }} · {{ duration(row.elapsed_ms) }} ·
          {{ row.attempts.length }} 次尝试</span>
      </button>
    </section>
    <div class="surface-card rounded-xl overflow-hidden">
      <div class="hidden md:block overflow-x-auto">
        <table class="w-full text-sm text-left whitespace-nowrap">
          <thead class="bg-slate-50 text-xs text-slate-500">
            <tr>
              <th class="p-4">
                时间 / 请求 ID
              </th>
              <th class="p-3">
                模型 / 协议
              </th>
              <th class="p-3">
                账号
              </th>
              <th class="p-3">
                结果 / HTTP
              </th>
              <th class="p-3 text-right">
                首包 / 总耗时
              </th>
              <th class="p-3 text-right">
                尝试 / Token
              </th>
              <th class="p-4" />
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            <tr
              v-for="row in rows"
              :key="row.request_id"
              class="hover:bg-slate-50/70"
            >
              <td class="p-4 text-xs">
                {{ new Date(row.started_at * 1000).toLocaleString() }}
                <div
                  class="text-slate-400 font-mono mt-1"
                  :title="row.request_id"
                >
                  {{ row.request_id.slice(0, 16) }}…
                </div>
              </td>
              <td class="p-3">
                <div class="font-mono text-xs">
                  {{ row.model || '未解析' }}
                </div>
                <div class="text-xs text-slate-400 mt-1">
                  {{ protocolLabels[row.protocol || ''] || row.protocol || '—' }} ·
                  {{ row.is_stream ? '流式' : '非流式' }}
                </div>
              </td>
              <td
                class="p-3 max-w-48 truncate text-xs"
                :title="row.account_name || row.account_id"
              >
                {{ row.account_name || row.account_id || '未调度' }}
              </td>
              <td class="p-3">
                <span
                  class="status-pill"
                  :class="resultClass(row.result)"
                >{{ resultLabels[row.result] || row.result }}</span><span
                  class="text-xs text-slate-400 ml-2"
                >{{ row.status_code || '—' }}</span>
              </td>
              <td class="p-3 text-right text-xs tabular-nums">
                {{ duration(row.first_byte_ms) }} / {{ duration(row.elapsed_ms) }}
              </td>
              <td class="p-3 text-right text-xs tabular-nums">
                {{ row.attempts.length }} 次
                <div class="text-slate-400 mt-1">
                  {{ row.usage.total_tokens ?? '—' }}
                </div>
              </td>
              <td class="p-4">
                <Button
                  size="xs"
                  @click="showDetail(row)"
                >
                  查看
                </Button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="md:hidden divide-y divide-slate-100">
        <button
          v-for="row in rows"
          :key="row.request_id"
          class="w-full p-4 text-left space-y-3 cursor-pointer hover:bg-slate-50"
          @click="showDetail(row)"
        >
          <div class="flex justify-between items-start gap-3">
            <span class="text-sm font-medium font-mono break-all">{{ row.model || '未解析模型' }}</span><span
              class="status-pill shrink-0"
              :class="resultClass(row.result)"
            >{{ resultLabels[row.result] || row.result }}</span>
          </div>
          <p class="text-xs text-slate-500 truncate">
            {{ row.account_name || row.account_id || '未调度' }}
          </p>
          <div class="flex flex-wrap justify-between gap-2 text-xs text-slate-500">
            <span>{{ new Date(row.started_at * 1000).toLocaleString() }}</span><span>{{ duration(row.elapsed_ms) }} · {{ row.attempts.length }} 次尝试 ·
              {{ row.usage.total_tokens ?? '—' }} Token</span>
          </div>
        </button>
      </div>
      <div
        v-if="!rows.length"
        class="p-12 text-center text-slate-400 text-sm"
      >
        {{ loading ? '正在加载请求记录…' : '当前筛选条件下暂无已完成请求' }}
      </div>
      <div
        class="flex flex-wrap justify-between items-center gap-2 p-4 border-t border-slate-100 text-xs text-slate-500"
      >
        <span>第 {{ page }} / {{ pages }} 页 · 每页 50 条</span>
        <div class="flex gap-2">
          <Button
            :disabled="page <= 1 || loading"
            @click="turn(-1)"
          >
            上一页
          </Button><Button
            :disabled="page >= pages || loading"
            @click="turn(1)"
          >
            下一页
          </Button>
        </div>
      </div>
    </div>
    <RequestDetail
      :request-id="selectedId"
      :key-names="options?.key_names"
      @close="closeDetail"
    />
  </div>
</template>
