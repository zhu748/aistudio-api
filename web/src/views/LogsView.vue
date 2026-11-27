<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { RefreshCw, Trash2, Search, ArrowDownToLine, Radio, Pause } from 'lucide-vue-next'
import HistoricalLogsPanel from '@/components/HistoricalLogsPanel.vue'
import { systemApi } from '@/api/system.ts'
import { useAuthStore } from '@/stores/auth.ts'
import { useToastStore } from '@/stores/toast.ts'
import type { LogEntry } from '@/types'
import { useRoute } from 'vue-router'
import { usePageRefresh } from '@/composables/usePageRefresh.ts'
import { errorMessage } from '@/utils/errors.ts'
import SegmentedControl from '@/components/ui/SegmentedControl.vue'
import LogRow from '@/components/logs/LogRow.vue'
import { useDebouncedWatch } from '@/composables/useDebouncedWatch.ts'

const route = useRoute()
const category = ref(typeof route.query.category === 'string' ? route.query.category : 'model')
const requestFilter = ref(String(route.query.request_id || ''))
const modelFilter = ref(String(route.query.model || ''))
const accountFilter = ref(String(route.query.account_id || ''))
const historical = ref(false)
const entries = ref<LogEntry[]>([])
const total = ref(0)
const capacity = ref(0)
const loadError = ref('')
const level = ref<string>('INFO')
const searchInput = ref(String(route.query.search || ''))
const search = ref(String(route.query.search || ''))
const order = ref<'desc' | 'asc'>('desc')
const live = ref(true)
const autoScroll = ref(false)
const connState = ref<'idle' | 'connecting' | 'open' | 'retrying'>('idle')

const MAX_LOCAL_ENTRIES = 1000
const SNAPSHOT_LIMIT = 500

const logContainer = ref<HTMLElement | null>(null)
const authStore = useAuthStore()
const toast = useToastStore()
let es: EventSource | null = null
let retryTimer: ReturnType<typeof setTimeout> | null = null
let connectSeq = 0

const levelOptions = [
  { value: 'DEBUG', label: 'DEBUG+' },
  { value: 'INFO', label: 'INFO+' },
  { value: 'WARNING', label: 'WARN+' },
  { value: 'ERROR', label: 'ERROR' },
]

const connDotClass: Record<string, string> = {
  idle: 'bg-slate-400',
  connecting: 'bg-amber-400 animate-pulse',
  open: 'bg-emerald-500 animate-pulse',
  retrying: 'bg-rose-500 animate-pulse',
}

const connLabel: Record<string, string> = {
  idle: '已暂停',
  connecting: '连接中…',
  open: 'SSE 实时流',
  retrying: '断线重连…',
}

const visibleLogs = computed(() => {
  const needle = search.value.trim().toLowerCase()
  let list = entries.value
  if (needle) {
    list = list.filter((e) =>
      `${e.message} ${e.name} ${e.level} ${e.request_id || ''} ${e.model || ''} ${e.account_id || ''} ${e.sig_meta ? JSON.stringify(e.sig_meta) : ''}`
        .toLowerCase()
        .includes(needle),
    )
  }
  return order.value === 'desc' ? [...list].reverse() : list
})

function applyFingerprintFilter(fp: string) {
  searchInput.value = fp
  search.value = fp
  if (category.value !== 'thought_signature') category.value = 'thought_signature'
}

const categoryOptions = [
  { value: 'model', label: '模型请求' },
  { value: 'thought_signature', label: 'ThoughtSignature' },
  { value: 'system', label: '系统日志' },
  { value: 'all', label: '全部日志' },
]

const orderOptions = [
  { value: 'desc', label: '最新在前' },
  { value: 'asc', label: '时间正序' },
]

const categoryDescriptions: Record<string, string> = {
  model: '仅展示模型调用及其签名、切号和网络异常',
  thought_signature: '专项追踪 thoughtSignature 客户端回传、温/冷缓存恢复、SKIP 兜底与上游收发指纹',
  system: '系统操作、后台任务与服务诊断',
  all: '模型请求、思维签名与系统诊断全量日志',
}

const keyMap = new WeakMap<LogEntry, number>()
let seqCounter = 0
function keyOf(entry: LogEntry): number {
  let key = keyMap.get(entry)
  if (key === undefined) {
    seqCounter += 1
    key = seqCounter
    keyMap.set(entry, key)
  }
  return key
}

function parseEventData<T>(ev: Event): T | null {
  try {
    const data = (ev as MessageEvent<string>).data
    return JSON.parse(data) as T
  } catch {
    return null
  }
}

interface StreamStats {
  total?: number
  size?: number
  capacity?: number
}

function applyStats(data: StreamStats | null) {
  if (!data) return
  if (typeof data.total === 'number') total.value = data.total
  else if (typeof data.size === 'number') total.value = data.size
  if (typeof data.capacity === 'number') capacity.value = data.capacity
}

function scrollToEnd() {
  requestAnimationFrame(() => {
    const el = logContainer.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function appendEntries(incoming: LogEntry[]) {
  if (!incoming.length) return
  const merged = entries.value.concat(incoming)
  entries.value = merged.length > MAX_LOCAL_ENTRIES ? merged.slice(-MAX_LOCAL_ENTRIES) : merged
  if (autoScroll.value && order.value === 'asc') scrollToEnd()
}

// SSE log 事件批量缓冲：逐条 appendEntries 会让 visibleLogs computed 每条都
// 重算（全量 filter+reverse）并触发上千行 v-for 重新 diff，高频日志下主线程
// 会被渲染打爆。这里攒批 ~100ms 后一次性 appendEntries；断流/卸载前先冲刷
// 余量，保证日志不丢且与后续 snapshot 顺序不乱。
const FLUSH_INTERVAL_MS = 100
const pendingEntries: LogEntry[] = []
let flushTimer: ReturnType<typeof setTimeout> | null = null

function flushPendingEntries() {
  if (flushTimer) {
    clearTimeout(flushTimer)
    flushTimer = null
  }
  if (!pendingEntries.length) return
  appendEntries(pendingEntries.splice(0, pendingEntries.length))
}

function queueLogEntry(entry: LogEntry) {
  pendingEntries.push(entry)
  if (flushTimer === null) {
    flushTimer = setTimeout(flushPendingEntries, FLUSH_INTERVAL_MS)
  }
}

function closeStream() {
  // 先冲刷缓冲余量：重连 snapshot 会整体替换 entries，未提交的旧流日志
  // 必须先落地，否则会在 snapshot 之后被追加造成乱序。
  flushPendingEntries()
  connectSeq += 1
  if (es) {
    es.close()
    es = null
  }
  if (retryTimer) {
    clearTimeout(retryTimer)
    retryTimer = null
  }
}

async function connectStream() {
  closeStream()
  const seq = connectSeq
  if (!live.value || historical.value) {
    connState.value = 'idle'
    return
  }
  connState.value = 'connecting'
  loadError.value = ''
  const params = new URLSearchParams()
  params.set('level', level.value)
  params.set('limit', String(SNAPSHOT_LIMIT))
  params.set('category', category.value)
  if (requestFilter.value.trim()) params.set('request_id', requestFilter.value.trim())
  if (modelFilter.value.trim()) params.set('model', modelFilter.value.trim())
  if (accountFilter.value.trim()) params.set('account_id', accountFilter.value.trim())

  const authRequired = authStore.authEnabled
  if (authRequired) {
    try {
      const res = await systemApi.createLogStreamTicket()
      if (!isCurrent(seq)) return
      if (!res.ticket) {
        loadError.value = '无法获取日志流访问票据'
        scheduleTicketRetry(seq)
        return
      }
      params.set('ticket', res.ticket)
    } catch {
      if (!isCurrent(seq)) return
      // 后端重启窗口内票据端点不可达：与 SSE 断开同款退避重连，
      // 原先直接放弃会让「实时」开关显示已暂停且不再自愈。
      loadError.value = '无法获取日志流访问票据，5 秒后自动重试'
      scheduleTicketRetry(seq)
      return
    }
  }

  if (!isCurrent(seq)) return
  const source = new EventSource(`/api/logs/stream?${params.toString()}`)
  es = source

  source.onopen = () => {
    if (!isCurrent(seq)) return
    connState.value = 'open'
  }
  source.addEventListener('ready', (ev) => {
    if (!isCurrent(seq)) return
    connState.value = 'open'
    applyStats(parseEventData<StreamStats>(ev))
  })
  source.addEventListener('snapshot', (ev) => {
    if (!isCurrent(seq)) return
    const data = parseEventData<{ logs?: LogEntry[] }>(ev)
    const logs = data?.logs
    entries.value = Array.isArray(logs) ? logs.slice(-MAX_LOCAL_ENTRIES) : []
    if (autoScroll.value && order.value === 'asc') scrollToEnd()
  })
  source.addEventListener('log', (ev) => {
    if (!isCurrent(seq)) return
    const entry = parseEventData<LogEntry>(ev)
    if (entry) queueLogEntry(entry)
  })
  source.addEventListener('ping', (ev) => {
    if (!isCurrent(seq)) return
    applyStats(parseEventData<StreamStats>(ev))
  })
  source.onerror = () => {
    if (!isCurrent(seq)) return
    if (source.readyState === EventSource.CLOSED) {
      if (!live.value) {
        connState.value = 'idle'
        return
      }
      connState.value = 'retrying'
      if (retryTimer) clearTimeout(retryTimer)
      retryTimer = setTimeout(() => {
        retryTimer = null
        void systemApi.getStats().catch(() => {})
        void connectStream()
      }, 5000)
    } else if (authRequired) {
      if (!live.value) {
        connState.value = 'idle'
        return
      }
      connState.value = 'retrying'
      source.close()
      if (retryTimer) clearTimeout(retryTimer)
      retryTimer = setTimeout(() => {
        retryTimer = null
        void connectStream()
      }, 5000)
    } else {
      connState.value = 'retrying'
    }
  }
}

function isCurrent(seq: number): boolean {
  return seq === connectSeq
}

/** 票据获取失败后的退避重连（与 SSE 断开同款 5s 节奏；代次守卫防陈旧重连） */
function scheduleTicketRetry(seq: number) {
  if (!isCurrent(seq) || !live.value) {
    connState.value = 'idle'
    return
  }
  connState.value = 'retrying'
  if (retryTimer) clearTimeout(retryTimer)
  retryTimer = setTimeout(() => {
    retryTimer = null
    void connectStream()
  }, 5000)
}

async function handleClear() {
  try {
    const res = await systemApi.clearLogs()
    // 丢弃已缓冲未提交的旧日志并取消定时冲刷，避免清空后又被余量回填
    pendingEntries.length = 0
    if (flushTimer) {
      clearTimeout(flushTimer)
      flushTimer = null
    }
    entries.value = []
    total.value = res.total ?? 0
    toast.success('环形日志缓冲区已清空')
  } catch (err: unknown) {
    const msg = errorMessage(err, '清空日志失败')
    toast.error(msg)
  }
}

function handleRefresh() {
  void connectStream()
}

// 页面隐藏时挂起 SSE（无人观看的日志渲染只是白白消耗 CPU 与带宽），
// 恢复可见时重连。与 onerror 自动重连互不打架：closeStream 会顺带清掉
// retryTimer 与在途连接（connectSeq 失配即作废），「由隐藏而挂起」只
// 有本处理函数会置位，重新可见时也只在该标志置位时恢复连接。
let pausedByHidden = false

function handleVisibilityChange() {
  if (document.hidden) {
    if (!live.value || historical.value) return
    // 没有活动流、待重连或在途连接时不需挂起（避免误吞 live 开关语义）
    if (!es && !retryTimer && connState.value !== 'connecting') return
    pausedByHidden = true
    closeStream()
    connState.value = 'idle'
  } else if (pausedByHidden) {
    pausedByHidden = false
    void connectStream()
  }
}

watch(historical, (value) => {
  if (value) {
    closeStream()
    connState.value = 'idle'
  } else void connectStream()
})
useDebouncedWatch(level, () => void connectStream(), { onChange: () => closeStream() })
useDebouncedWatch([category, requestFilter, modelFilter, accountFilter], () => void connectStream(), {
  onChange: () => {
    closeStream()
    entries.value = []
  },
})
watch(
  () => [route.query.request_id, route.query.category, route.query.search],
  ([reqId, cat, qSearch]) => {
    if (reqId !== undefined) requestFilter.value = String(reqId || '')
    if (typeof cat === 'string' && cat) category.value = cat
    if (qSearch !== undefined) {
      const val = String(qSearch || '')
      searchInput.value = val
      search.value = val
    }
  },
)
watch(live, (enabled) => {
  if (enabled) void connectStream()
  else {
    closeStream()
    connState.value = 'idle'
  }
})
watch(order, () => {
  if (autoScroll.value && order.value === 'asc') scrollToEnd()
})
useDebouncedWatch(searchInput, () => {
  search.value = searchInput.value
})

onMounted(() => {
  document.addEventListener('visibilitychange', handleVisibilityChange)
  void connectStream()
})
usePageRefresh(async () => {
  if (!historical.value) await connectStream()
  return historical.value || !loadError.value
})

onUnmounted(() => {
  closeStream()
  document.removeEventListener('visibilitychange', handleVisibilityChange)
})
</script>

<template>
  <div class="flex flex-col gap-3.5">
    <div class="surface-card rounded-xl p-4 space-y-3">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <SegmentedControl
          :model-value="category"
          :options="categoryOptions"
          container-class="flex flex-wrap gap-1 rounded-lg bg-slate-100 p-1"
          item-class="rounded-md px-3 py-1.5 text-sm cursor-pointer"
          active-class="bg-white text-brand-700 shadow-2xs font-semibold"
          idle-class="text-slate-500"
          @update:model-value="category = $event"
        />
        <p class="text-xs text-slate-500">
          {{ categoryDescriptions[category] || categoryDescriptions.all }}
        </p>
      </div>
      <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <label class="field-label">请求 ID<input
          v-model="requestFilter"
          class="field-input font-mono"
          placeholder="完整 Request ID"
        ></label><label class="field-label">模型<input
          v-model="modelFilter"
          class="field-input"
          placeholder="全部模型"
        ></label><label class="field-label">账号<input
          v-model="accountFilter"
          class="field-input"
          placeholder="账号 ID"
        ></label>
      </div>
    </div>
    <div class="flex gap-2 text-xs">
      <button
        type="button"
        class="px-3 py-2 rounded-lg border"
        :class="!historical ? 'bg-slate-900 text-white' : 'bg-white'"
        @click="historical = false"
      >
        实时日志
      </button><button
        type="button"
        class="px-3 py-2 rounded-lg border"
        :class="historical ? 'bg-slate-900 text-white' : 'bg-white'"
        @click="historical = true"
      >
        历史归档
      </button>
    </div>
    <HistoricalLogsPanel
      v-if="historical"
      :category="category"
      :request-id="requestFilter"
      :model="modelFilter"
      :account-id="accountFilter"
      :initial-search="search"
    />
    <template v-else>
      <!-- Consolidated Log Control Bar -->
      <div class="bg-white border border-slate-200/90 rounded-xl p-3.5 shadow-2xs space-y-3">
        <div class="flex flex-wrap items-center justify-between gap-3">
          <!-- Connection Status & Buffer Telemetry -->
          <div class="flex items-center gap-2.5 min-w-0">
            <span
              class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-xs font-semibold"
              :class="
                connState === 'open'
                  ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                  : connState === 'retrying'
                    ? 'bg-rose-50 text-rose-700 border-rose-200'
                    : 'bg-slate-100 text-slate-600 border-slate-200'
              "
            >
              <span
                class="w-2 h-2 rounded-full"
                :class="connDotClass[connState]"
              />
              <span>{{ connLabel[connState] }}</span>
            </span>

            <span class="text-xs text-slate-500 font-mono tabular-nums truncate">
              缓冲 <strong class="text-slate-800">{{ total }}</strong> / {{ capacity }} · 视图
              <strong class="text-slate-800">{{ visibleLogs.length }}</strong> 条
            </span>
          </div>

          <!-- Level Segmented Selector & Stream Actions -->
          <div class="flex items-center gap-2 flex-wrap">
            <!-- Level Pills -->
            <SegmentedControl
              :model-value="level"
              :options="levelOptions"
              container-class="flex items-center bg-slate-100 p-0.5 rounded-lg text-xs font-mono font-medium"
              active-class="bg-white text-slate-900 font-bold shadow-2xs"
              idle-class="text-slate-500 hover:text-slate-900"
              @update:model-value="level = $event"
            />

            <!-- Live Toggle Button -->
            <button
              type="button"
              class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-medium border transition-colors cursor-pointer"
              :class="
                live
                  ? 'bg-brand-50 text-brand-700 border-brand-200'
                  : 'bg-white text-slate-600 border-slate-200 hover:bg-slate-50'
              "
              @click="live = !live"
            >
              <Radio
                v-if="live"
                class="w-3.5 h-3.5"
              />
              <Pause
                v-else
                class="w-3.5 h-3.5"
              />
              <span>{{ live ? '实时订阅中' : '已暂停' }}</span>
            </button>

            <button
              type="button"
              class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs font-medium text-slate-600 bg-white border border-slate-200 hover:bg-slate-50 transition-colors cursor-pointer"
              :class="{ 'opacity-60 pointer-events-none': connState === 'connecting' }"
              @click="handleRefresh"
            >
              <RefreshCw
                class="w-3.5 h-3.5"
                :class="{ 'animate-spin': connState === 'connecting' }"
              />
              <span>重连</span>
            </button>

            <button
              type="button"
              class="inline-flex items-center gap-1 px-2.5 py-1 rounded-lg text-xs font-medium text-rose-600 hover:bg-rose-50 transition-colors cursor-pointer"
              @click="handleClear"
            >
              <Trash2 class="w-3.5 h-3.5" />
              <span>清空</span>
            </button>
          </div>
        </div>

        <!-- Second Row: Search Input & Sort Direction -->
        <div class="flex flex-wrap items-center gap-2.5 pt-2.5 border-t border-slate-100">
          <div class="relative flex-1 min-w-[200px]">
            <Search class="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-slate-400" />
            <input
              v-model="searchInput"
              type="text"
              :placeholder="
                category === 'thought_signature'
                  ? '过滤 8 位签名指纹 (fp)、载体、协议、模型、Request ID…'
                  : '过滤内容、模型、账号、Request ID…'
              "
              class="w-full bg-slate-50 border border-slate-200 rounded-lg pl-8 pr-3 py-1.5 text-xs font-mono text-slate-700 placeholder:text-slate-400 focus:bg-white focus:outline-none focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
            >
          </div>

          <SegmentedControl
            :model-value="order"
            :options="orderOptions"
            container-class="inline-flex rounded-lg bg-slate-100 p-0.5 text-xs font-medium"
            @update:model-value="order = $event as 'desc' | 'asc'"
          />

          <label
            v-if="order === 'asc'"
            class="flex items-center gap-1.5 text-xs text-slate-600 cursor-pointer select-none px-2 py-1 rounded-lg hover:bg-slate-50"
          >
            <ArrowDownToLine class="w-3.5 h-3.5 text-slate-400" />
            <input
              v-model="autoScroll"
              type="checkbox"
              class="accent-brand-600 w-3.5 h-3.5 cursor-pointer"
            >
            <span>跟随底部</span>
          </label>
        </div>
      </div>

      <!-- Connection Error Banner -->
      <div
        v-if="loadError"
        class="bg-rose-50 border border-rose-200 rounded-xl px-4 py-3 text-xs text-rose-800"
      >
        日志流连接失败：{{ loadError }}（请确认控制台登录状态与后端网关服务在线）
      </div>

      <!-- Terminal Log Stream Viewport -->
      <div
        ref="logContainer"
        class="bg-slate-950 rounded-xl border border-slate-800 p-3.5 font-mono text-xs leading-relaxed overflow-y-auto shadow-inner"
        style="max-height: calc(100vh - 280px); min-height: 360px"
      >
        <div
          v-if="visibleLogs.length === 0"
          class="h-full flex flex-col items-center justify-center gap-2 text-slate-500 py-16 font-sans"
        >
          <span class="text-sm font-medium text-slate-400">
            {{ connState === 'connecting' ? '正在接入实时日志流…' : '当前过滤条件下暂无日志' }}
          </span>
          <span
            v-if="connState !== 'connecting'"
            class="text-xs text-slate-600"
          >
            可切换上方级别至「DEBUG+」，或在「系统与协议配置 → 日志与调试」中调整输出级别
          </span>
        </div>

        <LogRow
          v-for="entry in visibleLogs"
          :key="keyOf(entry)"
          :entry="entry"
          @filter-fp="applyFingerprintFilter"
        />
      </div>
    </template>
  </div>
</template>
