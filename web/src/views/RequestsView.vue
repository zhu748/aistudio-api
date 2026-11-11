<script setup lang="ts">
import { computed, ref } from 'vue'
import { Download, RefreshCw, Search, Trash2 } from 'lucide-vue-next'
import { operationsApi, type RequestRecord } from '@/api/operations.ts'
import type { LogEntry } from '@/types'
import Button from '@/components/ui/Button.vue'
import Modal from '@/components/ui/Modal.vue'
import { usePolling } from '@/composables/usePolling.ts'
import { useConfirm } from '@/composables/useConfirm.ts'
import { useToastStore } from '@/stores/toast.ts'

const toast = useToastStore()
const { confirm } = useConfirm()
const rows = ref<RequestRecord[]>([])
const total = ref(0)
const page = ref(1)
const loading = ref(false)
const error = ref('')
const search = ref('')
const model = ref('')
const account = ref('')
const keyId = ref('')
const result = ref('')
const since = ref('')
const until = ref('')
const selected = ref<RequestRecord | null>(null)
const relatedLogs = ref<LogEntry[]>([])
const logsError = ref('')
const pages = computed(() => Math.max(1, Math.ceil(total.value / 50)))
const results: Record<string, string> = { success: '成功', error: '失败', rate_limited: '限流', cancelled: '已取消', timeout: '超时' }

function params() {
  const query = new URLSearchParams({ page: String(page.value), page_size: '50' })
  for (const [key, value] of Object.entries({ search: search.value, model: model.value, account_id: account.value, key_id: keyId.value, result: result.value })) {
    if (value.trim()) query.set(key, value.trim())
  }
  if (since.value) query.set('since', String(new Date(since.value).getTime() / 1000))
  if (until.value) query.set('until', String(new Date(until.value).getTime() / 1000))
  return query
}

async function load(reset = false) {
  if (loading.value) return
  if (reset) page.value = 1
  loading.value = true
  try {
    const response = await operationsApi.requests(params())
    rows.value = response.items
    total.value = response.total
    error.value = ''
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
  } finally {
    loading.value = false
  }
}

async function changePage(delta: number) {
  page.value += delta
  await load()
}

async function showDetail(record: RequestRecord) {
  selected.value = record
  relatedLogs.value = []
  logsError.value = ''
  try {
    const logs = await operationsApi.logs(new URLSearchParams({ request_id: record.request_id, level: 'DEBUG', page_size: '200' }))
    if (selected.value?.request_id === record.request_id) relatedLogs.value = logs.items
  } catch (err) {
    logsError.value = err instanceof Error ? err.message : '关联日志加载失败'
  }
}

async function exportRows() {
  try {
    const response = await operationsApi.exportRequests(params())
    const url = URL.createObjectURL(new Blob([JSON.stringify(response.items, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `requests-${new Date().toISOString().slice(0, 10)}.json`
    link.click()
    URL.revokeObjectURL(url)
    toast.success(`已导出 ${response.items.length} / ${response.total} 条记录（单次最多 10000 条）`)
  } catch (err) {
    toast.error(err instanceof Error ? err.message : '导出失败')
  }
}

async function clear() {
  if (!await confirm('清空全部请求历史？此操作无法恢复。')) return
  try {
    await operationsApi.clearRequests()
    await load(true)
  } catch (err) {
    toast.error(err instanceof Error ? err.message : '清空失败')
  }
}

function time(value: number) { return new Date(value * 1000).toLocaleString() }
function duration(value: number | null) { return value == null ? '—' : `${(value / 1000).toFixed(2)}s` }
usePolling(() => load(), 15000)
</script>

<template>
  <div class="space-y-4">
    <div class="flex flex-wrap items-center justify-between gap-3 bg-white border border-slate-200 rounded-xl p-4">
      <div>
        <h2 class="font-semibold text-slate-900">
          请求历史 <span class="text-brand-600 tabular-nums">{{ total }}</span>
        </h2>
        <p class="text-xs text-slate-500 mt-1">
          记录完整调用结果与重试链路，每 15 秒刷新。请求历史默认只存元数据；详细转储另行保存脱敏正文。
        </p>
      </div>
      <div class="flex gap-2">
        <Button
          :loading="loading"
          @click="load()"
        >
          <RefreshCw class="w-3.5 h-3.5" />刷新
        </Button>
        <Button @click="exportRows">
          <Download class="w-3.5 h-3.5" />导出
        </Button>
        <Button
          variant="danger"
          @click="clear"
        >
          <Trash2 class="w-3.5 h-3.5" />清空
        </Button>
      </div>
    </div>
    <form
      class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3 bg-white border border-slate-200 rounded-xl p-4"
      @submit.prevent="load(true)"
    >
      <input
        v-model="search"
        aria-label="搜索请求"
        placeholder="请求 ID、模型或账号"
        class="border border-slate-200 rounded-lg px-3 py-2 text-xs"
      >
      <input
        v-model="model"
        aria-label="模型"
        placeholder="模型（精确匹配）"
        class="border border-slate-200 rounded-lg px-3 py-2 text-xs"
      >
      <input
        v-model="account"
        aria-label="账号"
        placeholder="账号 ID"
        class="border border-slate-200 rounded-lg px-3 py-2 text-xs"
      >
      <input
        v-model="keyId"
        aria-label="密钥标识"
        placeholder="密钥标识（非密钥原文）"
        class="border border-slate-200 rounded-lg px-3 py-2 text-xs"
      >
      <select
        v-model="result"
        aria-label="结果"
        class="border border-slate-200 rounded-lg px-3 py-2 text-xs"
      >
        <option value="">
          全部结果
        </option><option
          v-for="(label, value) in results"
          :key="value"
          :value="value"
        >
          {{ label }}
        </option>
      </select>
      <label class="text-xs text-slate-500">开始时间<input
        v-model="since"
        type="datetime-local"
        class="block w-full border border-slate-200 rounded-lg px-2 py-1"
      ></label>
      <label class="text-xs text-slate-500">结束时间<input
        v-model="until"
        type="datetime-local"
        class="block w-full border border-slate-200 rounded-lg px-2 py-1"
      ></label>
      <Button
        type="submit"
        variant="primary"
      >
        <Search class="w-3.5 h-3.5" />筛选
      </Button>
    </form>
    <div
      v-if="error"
      role="alert"
      class="bg-rose-50 text-rose-700 rounded-lg p-3 text-sm"
    >
      {{ error }}
    </div>
    <div class="bg-white border border-slate-200 rounded-xl overflow-hidden">
      <div class="overflow-x-auto">
        <table class="w-full text-xs text-left whitespace-nowrap">
          <thead class="bg-slate-50 text-slate-500">
            <tr>
              <th class="p-3">
                时间 / 请求 ID
              </th><th class="p-3">
                模型 / 账号
              </th><th class="p-3">
                结果 / HTTP
              </th><th class="p-3">
                首包 / 总耗时
              </th><th class="p-3">
                尝试 / Token
              </th><th class="p-3">
                详情
              </th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            <tr
              v-for="row in rows"
              :key="row.request_id"
              class="hover:bg-slate-50"
            >
              <td class="p-3">
                {{ time(row.started_at) }}<div class="text-[10px] text-slate-400 font-mono mt-1">
                  {{ row.request_id.slice(0, 20) }}…
                </div>
              </td>
              <td class="p-3 font-mono">
                {{ row.model || '—' }}<div class="text-slate-400 mt-1">
                  {{ row.account_id || '未调度' }}
                </div>
              </td>
              <td class="p-3">
                <span
                  class="rounded px-2 py-1"
                  :class="row.result === 'success' ? 'bg-emerald-50 text-emerald-700' : 'bg-rose-50 text-rose-700'"
                >{{ results[row.result] || row.result }}</span><span class="ml-2 text-slate-500">{{ row.status_code }}</span>
              </td>
              <td class="p-3 tabular-nums">
                {{ duration(row.first_byte_ms) }} / {{ duration(row.elapsed_ms) }}
              </td>
              <td class="p-3">
                {{ row.attempts.length }} 次 / {{ row.usage.total_tokens ?? '—' }}
              </td>
              <td class="p-3">
                <Button
                  size="xs"
                  @click="showDetail(row)"
                >
                  查看
                </Button>
              </td>
            </tr>
            <tr v-if="!rows.length">
              <td
                colspan="6"
                class="p-12 text-center text-slate-400"
              >
                {{ loading ? '正在加载…' : '暂无请求记录。模型调用结束后会出现在这里。' }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div class="flex justify-between items-center p-3 border-t border-slate-100 text-xs text-slate-500">
        <span>第 {{ page }} / {{ pages }} 页 · 每页 50 条</span><div class="flex gap-2">
          <Button
            :disabled="page <= 1 || loading"
            @click="changePage(-1)"
          >
            上一页
          </Button><Button
            :disabled="page >= pages || loading"
            @click="changePage(1)"
          >
            下一页
          </Button>
        </div>
      </div>
    </div>
    <Modal
      :model-value="!!selected"
      title="请求详情"
      :subtitle="selected?.request_id"
      max-width="2xl"
      @update:model-value="selected = null"
    >
      <div
        v-if="selected"
        class="space-y-4 text-xs"
      >
        <dl class="grid grid-cols-2 gap-3 break-all">
          <div
            v-for="(value, label) in { '接口': selected.path, '密钥标识': selected.key_id || '匿名', '代理出口': selected.proxy || '直连', '签名节点': selected.signer || '—', '结束原因': selected.finish_reason || '—', '传输': selected.is_stream ? '流式' : '非流式' }"
            :key="label"
          >
            <dt class="text-slate-400 mb-1">
              {{ label }}
            </dt><dd>{{ value }}</dd>
          </div>
        </dl>
        <p
          v-if="selected.error"
          class="p-3 rounded bg-rose-50 text-rose-700 break-all"
        >
          {{ selected.error }}
        </p>
        <h3 class="font-semibold">
          上游尝试（{{ selected.attempts.length }}）
        </h3>
        <div
          v-for="(attempt, index) in selected.attempts"
          :key="index"
          class="p-3 rounded-lg border border-slate-200 space-y-1 break-all"
        >
          <p>#{{ index + 1 }} · {{ attempt.account_id }} · HTTP {{ attempt.status_code }} · {{ duration(attempt.elapsed_ms) }}</p><p class="text-slate-500">
            {{ attempt.proxy || '直连' }}
          </p><p
            v-if="attempt.error"
            class="text-rose-600"
          >
            {{ attempt.error }}
          </p>
        </div>
        <h3 class="font-semibold">
          Token 用量
        </h3><pre class="bg-slate-50 p-3 rounded-lg whitespace-pre-wrap">{{ JSON.stringify(selected.usage, null, 2) }}</pre>
        <h3 class="font-semibold">
          关联运行日志（最近 200 条）
        </h3>
        <p
          v-if="logsError"
          class="text-rose-600"
        >
          {{ logsError }}
        </p>
        <div class="bg-slate-950 text-slate-200 p-3 rounded-lg max-h-64 overflow-auto font-mono whitespace-pre-wrap break-all">
          <p
            v-for="(log, index) in relatedLogs"
            :key="index"
            class="py-1"
          >
            {{ log.ts }} [{{ log.level }}] {{ log.message }}
          </p><p
            v-if="!relatedLogs.length"
            class="text-slate-400"
          >
            暂无关联日志，或日志已轮转。
          </p>
        </div>
      </div>
    </Modal>
  </div>
</template>
