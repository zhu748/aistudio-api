<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from 'vue'
import { RefreshCw, Trash2, Search, ArrowDownToLine } from 'lucide-vue-next'
import { systemApi } from '@/api/system.ts'
import { getToken } from '@/api/client.ts'
import type { LogEntry } from '@/types'

// 本地日志列表：内部始终按时间正序维护；展示顺序由 order 控制（desc 时反转渲染）
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
// 连接状态：idle=已暂停 connecting=连接中 open=实时 retrying=重连中
const connState = ref<'idle' | 'connecting' | 'open' | 'retrying'>('idle')

// 本地保留的日志条数上限（超出后丢弃最旧，保持渲染性能）
const MAX_LOCAL_ENTRIES = 1000
// 建立连接时向后端请求的历史快照条数
const SNAPSHOT_LIMIT = 500

const logContainer = ref<HTMLElement | null>(null)
let es: EventSource | null = null
let retryTimer: ReturnType<typeof setTimeout> | null = null
let debounceTimer: ReturnType<typeof setTimeout> | null = null

const levelOptions = [
  { value: 'DEBUG', label: '全部 (DEBUG+)' },
  { value: 'INFO', label: 'INFO+' },
  { value: 'WARNING', label: '警告+' },
  { value: 'ERROR', label: '仅错误' },
]

const levelBadgeClass: Record<string, string> = {
  ERROR: 'bg-rose-50 text-rose-700 border-rose-200',
  CRITICAL: 'bg-rose-100 text-rose-800 border-rose-300',
  WARNING: 'bg-amber-50 text-amber-700 border-amber-200',
  INFO: 'bg-sky-50 text-sky-700 border-sky-200',
  DEBUG: 'bg-gray-100 text-gray-600 border-gray-200',
}

const rowAccentClass: Record<string, string> = {
  ERROR: 'border-l-2 border-l-rose-400',
  CRITICAL: 'border-l-2 border-l-rose-500',
  WARNING: 'border-l-2 border-l-amber-400',
}

const connDotClass: Record<string, string> = {
  idle: 'bg-gray-300',
  connecting: 'bg-amber-400 animate-pulse',
  open: 'bg-emerald-500 animate-pulse',
  retrying: 'bg-rose-400 animate-pulse',
}

const connLabel: Record<string, string> = {
  idle: '已暂停',
  connecting: '连接中…',
  open: '实时',
  retrying: '重连中…',
}

// 展示列表：关键字过滤在前端实时生效，排序仅影响渲染方向
const visibleLogs = computed(() => {
  const needle = search.value.trim().toLowerCase()
  let list = entries.value
  if (needle) {
    list = list.filter((e) => `${e.message} ${e.name} ${e.level}`.toLowerCase().includes(needle))
  }
  return order.value === 'desc' ? [...list].reverse() : list
})

// 稳定行键：按对象身份分配递增序号，避免 desc 模式下追加日志导致全列表重渲染
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
  if (es) {
    es.close()
    es = null
  }
  if (retryTimer) {
    clearTimeout(retryTimer)
    retryTimer = null
  }
}

function connectStream() {
  closeStream()
  if (!live.value) {
    connState.value = 'idle'
    return
  }
  connState.value = 'connecting'
  loadError.value = ''
  const params = new URLSearchParams()
  params.set('level', level.value)
  params.set('limit', String(SNAPSHOT_LIMIT))
  // EventSource 无法携带自定义请求头，通过 ?key= 查询参数传递管理密码（后端原生支持）
  const token = getToken()
  if (token) params.set('key', token)
  const source = new EventSource(`/api/logs/stream?${params.toString()}`)
  es = source

  source.onopen = () => {
    connState.value = 'open'
  }
  source.addEventListener('ready', (ev) => {
    connState.value = 'open'
    applyStats(parseEventData<StreamStats>(ev))
  })
  source.addEventListener('snapshot', (ev) => {
    const data = parseEventData<{ logs?: LogEntry[] }>(ev)
    // 快照整体替换本地列表（重连场景下天然去重）
    const logs = data?.logs
    entries.value = Array.isArray(logs) ? logs.slice(-MAX_LOCAL_ENTRIES) : []
    if (autoScroll.value && order.value === 'asc') scrollToEnd()
  })
  source.addEventListener('log', (ev) => {
    const entry = parseEventData<LogEntry>(ev)
    if (entry) appendEntries([entry])
  })
  source.addEventListener('ping', (ev) => {
    applyStats(parseEventData<StreamStats>(ev))
  })
  source.onerror = () => {
    if (!es) return
    if (es.readyState === EventSource.CLOSED) {
      // 连接被服务端拒绝或彻底关闭（如 401 / 服务停止）：退避后手动重连
      if (!live.value) {
        connState.value = 'idle'
        return
      }
      connState.value = 'retrying'
      if (retryTimer) clearTimeout(retryTimer)
      retryTimer = setTimeout(() => {
        // 顺带探测鉴权状态：token 失效时 client.ts 会自动跳转登录页
        void systemApi.getStats().catch(() => {})
        connectStream()
      }, 5000)
    } else {
      // 浏览器内建自动重连（网络抖动），重连成功后会收到新的快照
      connState.value = 'retrying'
    }
  }
}

async function handleClear() {
  try {
    const res = await systemApi.clearLogs()
    entries.value = []
    total.value = res.total ?? 0
  } catch {
    // 清空失败时静默处理，实时流会继续同步服务端状态
  }
}

function handleRefresh() {
  // 手动刷新 = 重新建立连接（服务端会重新下发最新快照）
  connectStream()
}

watch(level, () => connectStream())
watch(live, (enabled) => {
  if (enabled) connectStream()
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
  }, 400)
})

onMounted(() => {
  connectStream()
})

onUnmounted(() => {
  closeStream()
  if (debounceTimer) clearTimeout(debounceTimer)
})
</script>

<template>
  <div class="flex flex-col gap-4">
    <!-- 工具栏 -->
    <div class="bg-white border border-gray-200/80 rounded-2xl p-4 shadow-xs">
      <div class="flex flex-wrap items-center gap-3">
        <div class="flex items-center gap-2 min-w-0">
          <div class="w-2 h-2 rounded-full transition-colors" :class="connDotClass[connState]" />
          <span class="text-sm font-semibold text-gray-900">运行日志</span>
          <span class="text-[10px] font-medium px-1.5 py-px rounded border" :class="connState === 'open' ? 'bg-emerald-50 text-emerald-700 border-emerald-200' : 'bg-gray-50 text-gray-500 border-gray-200'">
            {{ connLabel[connState] }}
          </span>
          <span class="text-xs text-gray-400 font-mono truncate">
            缓冲 {{ total }} / {{ capacity }} 条 · 本地 {{ entries.length }} 条
          </span>
        </div>

        <div class="flex-1" />

        <label class="flex items-center gap-2 text-xs text-gray-600">
          <select
            v-model="level"
            class="border border-gray-200 rounded-lg px-2.5 py-1.5 text-xs bg-white text-gray-700 focus:outline-none focus:ring-2 focus:ring-brand-200 cursor-pointer"
          >
            <option v-for="opt in levelOptions" :key="opt.value" :value="opt.value">
              {{ opt.label }}
            </option>
          </select>
        </label>

        <label class="flex items-center gap-2 text-xs text-gray-600 cursor-pointer select-none">
          <input v-model="live" type="checkbox" class="accent-brand-500 w-3.5 h-3.5 cursor-pointer" />
          实时推送
        </label>

        <button
          type="button"
          class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium text-gray-600 hover:bg-gray-100 transition-colors cursor-pointer"
          :class="{ 'opacity-60 pointer-events-none': connState === 'connecting' }"
          @click="handleRefresh"
        >
          <RefreshCw class="w-3.5 h-3.5" :class="{ 'animate-spin': connState === 'connecting' }" />
          刷新
        </button>

        <button
          type="button"
          class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium text-rose-600 hover:bg-rose-50 transition-colors cursor-pointer"
          @click="handleClear"
        >
          <Trash2 class="w-3.5 h-3.5" />
          清空
        </button>
      </div>

      <div class="flex flex-wrap items-center gap-3 mt-3">
        <div class="relative flex-1 min-w-[200px]">
          <Search class="w-3.5 h-3.5 absolute left-3 top-1/2 -translate-y-1/2 text-gray-400" />
          <input
            v-model="searchInput"
            type="text"
            placeholder="搜索日志（消息 / 模块 / 级别）…"
            class="w-full border border-gray-200 rounded-lg pl-9 pr-3 py-1.5 text-xs text-gray-700 placeholder:text-gray-400 focus:outline-none focus:ring-2 focus:ring-brand-200"
          />
        </div>

        <div class="inline-flex rounded-lg border border-gray-200 overflow-hidden text-xs">
          <button
            type="button"
            class="px-3 py-1.5 font-medium transition-colors cursor-pointer"
            :class="order === 'desc' ? 'bg-brand-50 text-brand-700' : 'bg-white text-gray-600 hover:bg-gray-50'"
            @click="order = 'desc'"
          >
            最新在前
          </button>
          <button
            type="button"
            class="px-3 py-1.5 font-medium transition-colors cursor-pointer border-l border-gray-200"
            :class="order === 'asc' ? 'bg-brand-50 text-brand-700' : 'bg-white text-gray-600 hover:bg-gray-50'"
            @click="order = 'asc'"
          >
            时间正序
          </button>
        </div>

        <label
          v-if="order === 'asc'"
          class="flex items-center gap-1.5 text-xs text-gray-600 cursor-pointer select-none"
        >
          <ArrowDownToLine class="w-3.5 h-3.5 text-gray-400" />
          <input v-model="autoScroll" type="checkbox" class="accent-brand-500 w-3.5 h-3.5 cursor-pointer" />
          跟随滚动
        </label>
      </div>
    </div>

    <!-- 连接异常提示 -->
    <div
      v-if="loadError"
      class="bg-rose-50 border border-rose-200 rounded-2xl px-4 py-3 text-sm text-rose-700"
    >
      日志流连接失败：{{ loadError }}（请确认控制台鉴权状态与服务是否在线）
    </div>

    <!-- 日志列表 -->
    <div
      ref="logContainer"
      class="bg-gray-950 rounded-2xl border border-gray-800 p-4 font-mono text-xs leading-relaxed overflow-y-auto"
      style="max-height: calc(100vh - 320px); min-height: 320px"
    >
      <div v-if="visibleLogs.length === 0" class="h-full flex flex-col items-center justify-center gap-2 text-gray-500 py-12">
        <span class="text-sm">{{ connState === 'connecting' ? '正在连接实时日志流…' : '暂无匹配的日志' }}</span>
        <span v-if="connState !== 'connecting'" class="text-[11px] text-gray-600">
          空回复排查提示：调高上方级别到“全部 (DEBUG+)”，或检查系统配置中的日志级别设置
        </span>
      </div>

      <div
        v-for="entry in visibleLogs"
        :key="keyOf(entry)"
        class="py-1.5 px-2 rounded-md hover:bg-white/5"
        :class="rowAccentClass[entry.level]"
      >
        <div class="flex items-start gap-2 flex-wrap">
          <span class="text-gray-500 shrink-0 tabular-nums">{{ entry.ts }}</span>
          <span
            class="shrink-0 inline-flex items-center px-1.5 py-px rounded border text-[10px] font-semibold font-sans"
            :class="levelBadgeClass[entry.level] || levelBadgeClass.DEBUG"
          >
            {{ entry.level }}
          </span>
          <span class="text-gray-400 shrink-0 max-w-[220px] truncate" :title="entry.name">{{ entry.name }}</span>
        </div>
        <div class="text-gray-200 whitespace-pre-wrap break-all mt-0.5 pl-2">{{ entry.message }}</div>
      </div>
    </div>
  </div>
</template>
