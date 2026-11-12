<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Copy, Download, ExternalLink, RefreshCw } from 'lucide-vue-next'
import { operationsApi, type RequestRecord } from '@/api/operations.ts'
import type { LogEntry } from '@/types'
import Modal from '@/components/ui/Modal.vue'
import Button from '@/components/ui/Button.vue'
import { useClipboard } from '@/composables/useClipboard.ts'
import { usePolling } from '@/composables/usePolling.ts'
import {
  duration,
  resultClass,
  resultLabels,
  protocolLabels,
  stageLabels,
  downloadJson,
} from '@/utils/requests.ts'

const props = defineProps<{ requestId: string; keyNames?: Record<string, string> }>()
const emit = defineEmits<{ close: [] }>()
const { copy } = useClipboard()
const record = ref<RequestRecord | null>(null)
const loading = ref(false)
const error = ref('')
const logsError = ref('')
const payloadError = ref('')
const logs = ref<LogEntry[]>([])
const logsTotal = ref(0)
const logsPage = ref(1)
const logsLoading = ref(false)
const payload = ref<Awaited<ReturnType<typeof operationsApi.payload>> | null>(null)
const tab = ref('overview')
const detailUrl = computed(
  () => `${window.location.origin}/requests?request_id=${encodeURIComponent(props.requestId)}`,
)
const thinkingRequested = computed(() => {
  const parameters = record.value?.request_parameters
  if (!parameters) return '未记录（旧请求）'
  const effort = parameters.reasoning_effort || parameters.reasoningEffort
  if (effort) return String(effort)
  const config = parameters.thinkingConfig || parameters.thinking
  return config ? JSON.stringify(config) : '未指定，使用服务配置'
})
const effectiveThinking = computed(() => {
  const parameters = record.value?.effective_parameters
  if (!parameters || !Object.keys(parameters).length) return '尚未解析或旧请求未记录'
  const parts = []
  if (parameters.thinking_level != null) parts.push(`强度 ${parameters.thinking_level}`)
  if (parameters.thinking_budget != null)
    parts.push(`预算 ${parameters.thinking_budget === -1 ? '自动' : parameters.thinking_budget}`)
  return (
    parts.join(' · ') ||
    (parameters.thinking_mode != null ? `思考模式 ${parameters.thinking_mode}` : '未注入思考配置')
  )
})
const tabs = [
  { id: 'overview', label: '调用概览' },
  { id: 'timeline', label: '执行过程' },
  { id: 'payload', label: '请求与响应' },
  { id: 'logs', label: '关联日志' },
]
let sequence = 0

async function loadLogs() {
  const id = props.requestId
  const page = logsPage.value
  logsLoading.value = true
  try {
    const response = await operationsApi.logs(
      new URLSearchParams({
        request_id: id,
        category: 'model',
        level: 'DEBUG',
        order: 'asc',
        page: String(page),
        page_size: '100',
      }),
    )
    if (id !== props.requestId || page !== logsPage.value) return
    logs.value = response.items
    logsTotal.value = response.total
    logsError.value = ''
  } catch (err) {
    if (id === props.requestId) logsError.value = err instanceof Error ? err.message : '关联日志加载失败'
  } finally {
    if (id === props.requestId) logsLoading.value = false
  }
}

async function load() {
  const id = props.requestId
  if (!id) return
  const current = ++sequence
  loading.value = true
  await Promise.allSettled([
    operationsApi
      .detail(id)
      .then((value) => {
        if (current === sequence) {
          record.value = value
          error.value = ''
        }
      })
      .catch((err) => {
        if (current === sequence) error.value = err instanceof Error ? err.message : '详情加载失败'
      }),
    operationsApi
      .payload(id)
      .then((value) => {
        if (current === sequence) {
          payload.value = value
          payloadError.value = ''
        }
      })
      .catch((err) => {
        if (current === sequence) payloadError.value = err instanceof Error ? err.message : '正文加载失败'
      }),
    loadLogs(),
  ])
  if (current === sequence) loading.value = false
}

watch(
  () => props.requestId,
  () => {
    sequence++
    record.value = null
    payload.value = null
    logs.value = []
    error.value = ''
    logsError.value = ''
    payloadError.value = ''
    logsPage.value = 1
    tab.value = 'overview'
    if (props.requestId) void load()
  },
  { immediate: true },
)
usePolling(
  async () => {
    if (props.requestId && record.value?.result === 'running' && !loading.value) await load()
  },
  5000,
  { immediate: false },
)
const usage = computed(() => {
  const raw = record.value?.usage || {}
  const details = raw.completion_tokens_details as Record<string, unknown> | undefined
  return {
    输入: raw.prompt_tokens,
    输出: raw.completion_tokens,
    思考: details?.reasoning_tokens,
    缓存: raw.cached_tokens,
    总计: raw.total_tokens,
  }
})
function value(number: unknown) {
  return typeof number === 'number' ? number.toLocaleString() : '—'
}
async function turnLogs(delta: number) {
  logsPage.value += delta
  await loadLogs()
}
</script>

<template>
  <Modal
    :model-value="!!requestId"
    title="请求详情"
    :subtitle="requestId"
    max-width="4xl"
    @update:model-value="emit('close')"
  >
    <p
      v-if="error"
      role="alert"
      class="rounded-lg bg-rose-50 text-rose-700 p-3 text-sm"
    >
      {{ error }}
    </p>
    <p
      v-if="!record && loading"
      class="py-12 text-center text-slate-500 text-sm"
    >
      正在加载调用详情…
    </p>
    <div
      v-if="record"
      class="space-y-5"
    >
      <div class="flex flex-wrap items-center justify-between gap-3">
        <span
          class="status-pill"
          :class="resultClass(record.result)"
        >{{ resultLabels[record.result] || record.result
        }}<span
          v-if="record.result === 'running'"
          class="ml-2"
        >{{ stageLabels[record.stage || ''] || record.stage }}</span></span>
        <div class="flex flex-wrap gap-2">
          <Button
            size="xs"
            @click="copy(requestId, 'id')"
          >
            <Copy class="w-3.5 h-3.5" />请求 ID
          </Button><Button
            size="xs"
            @click="copy(detailUrl, 'link')"
          >
            <ExternalLink class="w-3.5 h-3.5" />详情链接
          </Button><Button
            size="xs"
            @click="downloadJson(record, `${requestId}.json`)"
          >
            <Download class="w-3.5 h-3.5" />导出
          </Button><Button
            size="xs"
            :loading="loading"
            @click="load"
          >
            <RefreshCw class="w-3.5 h-3.5" />刷新
          </Button>
        </div>
      </div>
      <div
        v-if="record.error"
        class="rounded-lg bg-rose-50 border border-rose-100 p-3 text-sm text-rose-700 break-words"
      >
        {{ record.error }}
      </div>
      <div class="flex gap-1 border-b border-slate-200 overflow-x-auto">
        <button
          v-for="item in tabs"
          :key="item.id"
          class="px-3 py-2.5 text-sm whitespace-nowrap border-b-2 cursor-pointer"
          :class="
            tab === item.id
              ? 'border-brand-600 text-brand-700 font-semibold'
              : 'border-transparent text-slate-500'
          "
          @click="tab = item.id"
        >
          {{ item.label }}
        </button>
      </div>
      <div
        v-if="tab === 'overview'"
        class="space-y-5"
      >
        <div
          class="rounded-xl border border-brand-100 bg-brand-50/40 p-4 grid grid-cols-1 sm:grid-cols-2 gap-4 text-sm"
        >
          <div>
            <p class="text-xs text-slate-500 mb-1">
              客户端指定的思考配置
            </p>
            <p class="break-all font-medium">
              {{ thinkingRequested }}
            </p>
          </div>
          <div>
            <p class="text-xs text-slate-500 mb-1">
              实际发送的思考配置
            </p>
            <p class="font-medium">
              {{ effectiveThinking }}
            </p>
          </div>
          <div class="sm:col-span-2 flex flex-wrap gap-x-5 gap-y-2 text-xs text-slate-600">
            <span
              v-for="(key, label) in {
                Temperature: 'temperature',
                'Top P': 'top_p',
                'Top K': 'top_k',
                '输出 Token 上限': 'max_tokens',
              }"
              :key="key"
            >{{ label }}：{{ record.effective_parameters?.[key] ?? '上游默认 / 未记录' }}</span>
          </div>
        </div>
        <div class="grid grid-cols-2 sm:grid-cols-4 gap-2">
          <div
            v-for="(amount, label) in {
              首包时间: record.first_byte_ms,
              首次思考输出: record.first_thought_ms,
              首段回复时间: record.first_content_ms,
              总回复耗时: record.elapsed_ms,
            }"
            :key="label"
            class="rounded-lg bg-slate-50 p-3"
          >
            <p class="text-xs text-slate-500">
              {{ label }}
            </p>
            <p class="font-semibold tabular-nums mt-1">
              {{ duration(amount) }}
            </p>
          </div>
        </div>
        <dl class="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-4 text-sm">
          <div>
            <dt class="text-xs text-slate-500 mb-1">
              客户端模型 → 实际模型
            </dt>
            <dd class="font-mono text-xs break-all">
              {{ record.requested_model || record.model || '未解析' }} → {{ record.model || '未解析' }}
            </dd>
          </div>
          <div>
            <dt class="text-xs text-slate-500 mb-1">
              账号
            </dt>
            <dd class="break-all">
              <router-link
                v-if="record.account_id"
                :to="{ path: '/accounts', query: { account_id: record.account_id } }"
                class="text-brand-700 hover:underline"
              >
                {{ record.account_name || record.account_id }}
              </router-link><span v-else>尚未调度</span>
            </dd>
          </div>
          <div
            v-for="(item, label) in {
              开始时间: new Date(record.started_at * 1000).toLocaleString(),
              '协议 / 传输': `${protocolLabels[record.protocol || ''] || record.protocol || '未知'} / ${record.is_stream ? '流式' : '非流式'}`,
              'HTTP 状态':
                record.status_code || (record.result === 'running' ? '等待响应' : '未取得响应状态'),
              '首包 / 总耗时': `${duration(record.first_byte_ms)} / ${duration(record.elapsed_ms)}`,
              上游尝试: `${record.attempts.length} 次`,
              密钥: keyNames?.[record.key_id] || record.key_id || '匿名',
              结束原因: record.finish_reason || '—',
              代理出口: record.proxy || '直连',
              签名节点: record.signer || '—',
              接口: record.path,
            }"
            :key="label"
          >
            <dt class="text-xs text-slate-500 mb-1">
              {{ label }}
            </dt>
            <dd class="break-all">
              {{ item }}
            </dd>
          </div>
        </dl>
        <div class="grid grid-cols-2 sm:grid-cols-5 gap-2">
          <div
            v-for="(amount, label) in usage"
            :key="label"
            class="rounded-lg bg-slate-50 p-3"
          >
            <p class="text-xs text-slate-500">
              {{ label }} Token
            </p>
            <p class="font-semibold tabular-nums mt-1 text-sm">
              {{ value(amount) }}
            </p>
          </div>
        </div>
        <p class="text-xs text-slate-500">
          流式响应的 HTTP 200 表示连接已建立，最终结果以本条记录的状态为准。
        </p>
        <p class="text-xs text-slate-500">
          首包为发送给客户端的首个响应数据；首段回复为应用首次收到正文、图片或工具调用的时间。思考输出单独计时，非流式请求需等待完整响应。未提供的
          Token 显示为「—」。
        </p>
      </div>
      <div
        v-else-if="tab === 'timeline'"
        class="space-y-5"
      >
        <ol
          v-if="record.events?.length"
          class="border-l-2 border-slate-200 ml-2 space-y-4"
        >
          <li
            v-for="(event, index) in record.events"
            :key="index"
            class="relative pl-5 text-sm"
          >
            <span class="absolute w-2.5 h-2.5 rounded-full bg-brand-500 -left-1.5 top-1" />
            <div class="flex justify-between gap-3">
              <strong class="font-medium">{{ stageLabels[event.stage] || event.stage }}</strong><span
                class="text-xs text-slate-500 tabular-nums"
              >+{{ duration(event.elapsed_ms) }}</span>
            </div>
            <p class="text-slate-500 text-xs mt-1 break-words">
              {{ event.message }}<span v-if="event.account_id"> · {{ event.account_id }}</span>
            </p>
          </li>
        </ol>
        <p
          v-else
          class="text-sm text-slate-500"
        >
          此记录生成于执行时间线启用前。
        </p>
        <h3 class="text-sm font-semibold">
          上游尝试 · {{ record.attempts.length }}
        </h3>
        <div
          v-for="(attempt, index) in record.attempts"
          :key="index"
          class="p-3 border border-slate-200 rounded-lg text-sm space-y-1"
        >
          <p>
            #{{ index + 1 }} · {{ attempt.account_id || '未知账号' }} · HTTP
            {{ attempt.status_code || '未取得响应' }} · {{ duration(attempt.elapsed_ms)
            }}<span
              v-if="attempt.error_code"
              class="ml-2 text-rose-600"
            >业务错误码 {{ attempt.error_code }}</span>
          </p>
          <p class="text-xs text-slate-500 break-all">
            {{ attempt.proxy || '直连' }}
          </p>
          <p
            v-if="attempt.error"
            class="text-rose-600 text-xs break-words"
          >
            {{ attempt.error }}
          </p>
        </div>
        <p
          v-if="!record.attempts.length"
          class="text-sm text-slate-500"
        >
          尚未向上游发送请求；可结合执行阶段判断是否停在校验、选号或签名阶段。
        </p>
      </div>
      <div
        v-else-if="tab === 'payload'"
        class="space-y-4 text-sm"
      >
        <p
          v-if="payloadError"
          class="text-rose-600"
        >
          {{ payloadError }}
        </p>
        <p class="text-slate-500">
          {{ payload?.note || '正在查询正文采集状态…' }}
        </p>
        <template v-if="payload?.available">
          <div
            v-for="(body, label) in {
              请求正文: payload.payload?.body,
              响应正文: payload.payload?.response_body,
            }"
            :key="label"
          >
            <h3 class="font-medium mb-2">
              {{ label }}
            </h3>
            <pre
              class="rounded-lg bg-slate-950 text-slate-200 p-4 text-xs overflow-auto max-h-80 whitespace-pre-wrap break-all"
            >{{ body == null ? '未采集或已省略' : JSON.stringify(body, null, 2) }}</pre>
          </div>
          <p
            v-if="payload.payload?.request_body_truncated || payload.payload?.response_body_truncated"
            class="text-amber-700"
          >
            部分正文超过采集上限，已省略。
          </p>
        </template>
        <router-link
          v-else
          to="/settings"
          class="text-brand-700 hover:underline"
        >
          查看日志与转储设置
        </router-link>
        <details>
          <summary class="cursor-pointer text-slate-500">
            原始用量元数据
          </summary>
          <pre class="mt-2 p-3 bg-slate-50 rounded-lg text-xs whitespace-pre-wrap">{{
            JSON.stringify(record.usage, null, 2)
          }}</pre>
        </details>
      </div>
      <div
        v-else
        class="space-y-3"
      >
        <div class="flex flex-wrap justify-between gap-2 text-xs text-slate-500">
          <span>按时间正序 · {{ logsTotal }} 条</span><router-link
            :to="{ path: '/logs', query: { request_id: requestId } }"
            class="text-brand-700"
          >
            在运行日志中打开
          </router-link>
        </div>
        <p
          v-if="logsError"
          class="text-sm text-rose-600"
        >
          {{ logsError }}
        </p>
        <div
          class="bg-slate-950 text-slate-200 p-4 rounded-lg max-h-96 overflow-auto font-mono text-xs whitespace-pre-wrap break-words"
        >
          <p
            v-for="(log, index) in logs"
            :key="index"
            class="py-2 border-b border-slate-800"
          >
            <span class="text-slate-500">{{ log.ts }}</span>
            <span :class="log.level === 'ERROR' ? 'text-rose-400' : 'text-sky-400'">[{{ log.level }}]</span>
            {{ log.message }}
          </p>
          <p
            v-if="!logs.length"
            class="text-slate-400 py-8 text-center"
          >
            {{ logsLoading ? '加载中…' : '暂无关联日志，或归档已轮转。' }}
          </p>
        </div>
        <div class="flex justify-between items-center text-xs text-slate-500">
          <span>第 {{ logsPage }} / {{ Math.max(1, Math.ceil(logsTotal / 100)) }} 页</span>
          <div class="flex gap-2">
            <Button
              size="xs"
              :disabled="logsPage <= 1 || logsLoading"
              @click="turnLogs(-1)"
            >
              上一页
            </Button><Button
              size="xs"
              :disabled="logsPage * 100 >= logsTotal || logsLoading"
              @click="turnLogs(1)"
            >
              下一页
            </Button>
          </div>
        </div>
      </div>
    </div>
  </Modal>
</template>
