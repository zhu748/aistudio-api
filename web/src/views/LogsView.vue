<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { RefreshCw, Trash2, Search, ArrowDownToLine, Radio, Pause } from 'lucide-vue-next'
import HistoricalLogsPanel from '@/components/HistoricalLogsPanel.vue'
import { systemApi } from '@/api/system.ts'
import { useAuthStore } from '@/stores/auth.ts'
import { useToastStore } from '@/stores/toast.ts'
import type { LogEntry } from '@/types'
import { useRoute } from 'vue-router'
import { stageLabels } from '@/utils/requests.ts'
import { usePageRefresh } from '@/composables/usePageRefresh.ts'

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
const searchInput = ref('')
const search = ref('')
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
let debounceTimer: ReturnType<typeof setTimeout> | null = null
let connectSeq = 0

const levelOptions = [
  { value: 'DEBUG', label: 'DEBUG+' },
  { value: 'INFO', label: 'INFO+' },
  { value: 'WARNING', label: 'WARN+' },
  { value: 'ERROR', label: 'ERROR' },
]

const levelBadgeClass: Record<string, string> = {
  ERROR: 'bg-rose-500/15 text-rose-400 border-rose-500/30',
  CRITICAL: 'bg-rose-500/25 text-rose-300 border-rose-500/40',
  WARNING: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  INFO: 'bg-sky-500/15 text-sky-300 border-sky-500/30',
  DEBUG: 'bg-slate-800 text-slate-400 border-slate-700',
}

const rowAccentClass: Record<string, string> = {
  ERROR: 'border-l-2 border-l-rose-500 bg-rose-950/20',
  CRITICAL: 'border-l-2 border-l-rose-500 bg-rose-950/30',
  WARNING: 'border-l-2 border-l-amber-400 bg-amber-950/15',
}

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
      `${e.message} ${e.name} ${e.level} ${e.request_id || ''} ${e.model || ''} ${e.account_id || ''}`
        .toLowerCase()
        .includes(needle),
    )
  }
  return order.value === 'desc' ? [...list].reverse() : list
})

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

function closeStream() {
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
        connState.value = 'idle'
        return
      }
      params.set('ticket', res.ticket)
    } catch {
      if (!isCurrent(seq)) return
      loadError.value = '无法获取日志流访问票据，请确认登录状态'
      connState.value = 'idle'
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
    if (entry) appendEntries([entry])
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

async function handleClear() {
  try {
    const res = await systemApi.clearLogs()
    entries.value = []
    total.value = res.total ?? 0
    toast.success('环形日志缓冲区已清空')
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '清空日志失败'
    toast.error(msg)
  }
}

function handleRefresh() {
  void connectStream()
}

watch(historical, (value) => {
  if (value) {
    closeStream()
    connState.value = 'idle'
  } else void connectStream()
})
watch(level, () => void connectStream())
let filterTimer: ReturnType<typeof setTimeout> | null = null
watch([category, requestFilter, modelFilter, accountFilter], () => {
  closeStream()
  entries.value = []
  if (filterTimer) clearTimeout(filterTimer)
  filterTimer = setTimeout(() => void connectStream(), 300)
})
watch(
  () => route.query.request_id,
  (value) => {
    requestFilter.value = String(value || '')
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
watch(searchInput, (val) => {
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    search.value = val
  }, 300)
})

onMounted(() => {
  void connectStream()
})
usePageRefresh(async () => {
  if (!historical.value) await connectStream()
  return historical.value || !loadError.value
})

onUnmounted(() => {
  closeStream()
  if (debounceTimer) clearTimeout(debounceTimer)
  if (filterTimer) clearTimeout(filterTimer)
})
</script>

<template>
  <div class="flex flex-col gap-3.5">
    <div class="surface-card rounded-xl p-4 space-y-3">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div class="flex gap-1 rounded-lg bg-slate-100 p-1">
          <button
            v-for="item in [
              { id: 'model', label: '模型请求' },
              { id: 'system', label: '系统日志' },
              { id: 'all', label: '全部日志' },
            ]"
            :key="item.id"
            class="rounded-md px-3 py-1.5 text-sm cursor-pointer"
            :class="
              category === item.id ? 'bg-white text-brand-700 shadow-2xs font-semibold' : 'text-slate-500'
            "
            @click="category = item.id"
          >
            {{ item.label }}
          </button>
        </div>
        <p class="text-xs text-slate-500">
          {{
            category === 'model' ? '仅展示模型调用及其签名、切号和网络异常' : '系统操作、后台任务与服务诊断'
          }}
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
            <div class="flex items-center bg-slate-100 p-0.5 rounded-lg text-xs font-mono font-medium">
              <button
                v-for="opt in levelOptions"
                :key="opt.value"
                type="button"
                class="px-2.5 py-1 rounded-md transition-all cursor-pointer"
                :class="
                  level === opt.value
                    ? 'bg-white text-slate-900 font-bold shadow-2xs'
                    : 'text-slate-500 hover:text-slate-900'
                "
                @click="level = opt.value"
              >
                {{ opt.label }}
              </button>
            </div>

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
              placeholder="过滤内容、模型、账号、Request ID…"
              class="w-full bg-slate-50 border border-slate-200 rounded-lg pl-8 pr-3 py-1.5 text-xs font-mono text-slate-700 placeholder:text-slate-400 focus:bg-white focus:outline-none focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
            >
          </div>

          <div class="inline-flex rounded-lg bg-slate-100 p-0.5 text-xs font-medium">
            <button
              type="button"
              class="px-2.5 py-1 rounded-md transition-all cursor-pointer"
              :class="
                order === 'desc'
                  ? 'bg-white text-slate-900 font-semibold shadow-2xs'
                  : 'text-slate-500 hover:text-slate-900'
              "
              @click="order = 'desc'"
            >
              最新在前
            </button>
            <button
              type="button"
              class="px-2.5 py-1 rounded-md transition-all cursor-pointer"
              :class="
                order === 'asc'
                  ? 'bg-white text-slate-900 font-semibold shadow-2xs'
                  : 'text-slate-500 hover:text-slate-900'
              "
              @click="order = 'asc'"
            >
              时间正序
            </button>
          </div>

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

        <div
          v-for="entry in visibleLogs"
          :key="keyOf(entry)"
          class="py-1.5 px-2.5 rounded hover:bg-white/[0.04] transition-colors"
          :class="rowAccentClass[entry.level]"
        >
          <div class="flex items-baseline gap-2 flex-wrap">
            <span class="text-slate-500 shrink-0 tabular-nums text-[11px]">{{ entry.ts }}</span>
            <span
              class="shrink-0 inline-flex items-center px-1.5 py-px rounded border text-[10px] font-semibold"
              :class="levelBadgeClass[entry.level] || levelBadgeClass.DEBUG"
            >
              {{ entry.level }}
            </span>
            <span
              v-if="entry.category !== 'model'"
              class="text-brand-400/90 shrink-0 max-w-[240px] truncate text-[11px]"
              :title="entry.name"
            >{{ entry.name }}</span>
            <span
              v-if="entry.model"
              class="text-brand-300 text-xs break-all"
            >{{ entry.model }}</span>
            <span
              v-if="entry.account_id"
              class="text-slate-400 text-xs"
            >{{ entry.account_id }}</span>
            <span
              v-if="entry.stage"
              class="text-slate-500 text-xs"
            >{{ stageLabels[entry.stage] || entry.stage }}</span>
            <router-link
              v-if="entry.request_id && entry.category === 'model'"
              :to="{ path: '/requests', query: { request_id: entry.request_id } }"
              class="text-sky-400 hover:underline text-xs font-mono"
              :title="entry.request_id"
            >
              {{ entry.request_id.slice(0, 16) }}… ↗
            </router-link>
            <span class="text-slate-200 whitespace-pre-wrap break-words w-full leading-6 text-[13px]">
              {{ entry.message }}
            </span>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>
