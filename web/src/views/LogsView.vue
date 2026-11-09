<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted } from 'vue'
import { RefreshCw, Trash2, Search, ArrowDownToLine } from 'lucide-vue-next'
import { systemApi } from '@/api/system.ts'
import type { LogEntry } from '@/types'

const logs = ref<LogEntry[]>([])
const total = ref(0)
const capacity = ref(0)
const loading = ref(false)
const loadError = ref('')
const level = ref<string>('INFO')
const searchInput = ref('')
const search = ref('')
const order = ref<'desc' | 'asc'>('desc')
const autoRefresh = ref(true)
const autoScroll = ref(false)

const logContainer = ref<HTMLElement | null>(null)
let pollTimer: ReturnType<typeof setInterval> | null = null
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

async function fetchLogs() {
  if (loading.value) return
  loading.value = true
  loadError.value = ''
  try {
    const res = await systemApi.getLogs({
      level: level.value,
      limit: 500,
      search: search.value,
      order: order.value,
    })
    logs.value = res.logs
    total.value = res.total
    capacity.value = res.capacity
    if (autoScroll.value && order.value === 'asc') {
      requestAnimationFrame(() => {
        const el = logContainer.value
        if (el) el.scrollTop = el.scrollHeight
      })
    }
  } catch (e) {
    loadError.value = e instanceof Error ? e.message : '日志加载失败'
  } finally {
    loading.value = false
  }
}

async function handleClear() {
  try {
    await systemApi.clearLogs()
    logs.value = []
    total.value = 0
  } catch {
    // 清空失败时静默处理，下一次轮询会自动恢复
  }
  await fetchLogs()
}

function stopPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function restartPolling() {
  stopPolling()
  if (autoRefresh.value) {
    pollTimer = setInterval(fetchLogs, 3000)
  }
}

watch(level, () => fetchLogs())
watch(order, () => fetchLogs())
watch(autoRefresh, () => restartPolling())
watch(searchInput, (val) => {
  if (debounceTimer) clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => {
    search.value = val
    fetchLogs()
  }, 400)
})

onMounted(() => {
  fetchLogs()
  restartPolling()
})

onUnmounted(() => {
  stopPolling()
  if (debounceTimer) clearTimeout(debounceTimer)
})
</script>

<template>
  <div class="flex flex-col gap-4">
    <!-- 工具栏 -->
    <div class="bg-white border border-gray-200/80 rounded-2xl p-4 shadow-xs">
      <div class="flex flex-wrap items-center gap-3">
        <div class="flex items-center gap-2 min-w-0">
          <div class="w-2 h-2 rounded-full transition-colors" :class="autoRefresh ? 'bg-emerald-500 animate-pulse' : 'bg-gray-300'" />
          <span class="text-sm font-semibold text-gray-900">运行日志</span>
          <span class="text-xs text-gray-400 font-mono truncate">
            缓冲 {{ total }} / {{ capacity }} 条
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
          <input v-model="autoRefresh" type="checkbox" class="accent-brand-500 w-3.5 h-3.5 cursor-pointer" />
          自动刷新
        </label>

        <button
          type="button"
          class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium text-gray-600 hover:bg-gray-100 transition-colors cursor-pointer"
          :class="{ 'opacity-60 pointer-events-none': loading }"
          @click="fetchLogs"
        >
          <RefreshCw class="w-3.5 h-3.5" :class="{ 'animate-spin': loading }" />
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

    <!-- 加载失败提示 -->
    <div
      v-if="loadError"
      class="bg-rose-50 border border-rose-200 rounded-2xl px-4 py-3 text-sm text-rose-700"
    >
      日志加载失败：{{ loadError }}（请确认控制台鉴权状态与服务是否在线）
    </div>

    <!-- 日志列表 -->
    <div
      ref="logContainer"
      class="bg-gray-950 rounded-2xl border border-gray-800 p-4 font-mono text-xs leading-relaxed overflow-y-auto"
      style="max-height: calc(100vh - 320px); min-height: 320px"
    >
      <div v-if="logs.length === 0" class="h-full flex flex-col items-center justify-center gap-2 text-gray-500 py-12">
        <span class="text-sm">暂无匹配的日志</span>
        <span class="text-[11px] text-gray-600">
          空回复排查提示：调高上方级别到“全部 (DEBUG+)”，或检查系统配置中的日志级别设置
        </span>
      </div>

      <div
        v-for="(entry, idx) in logs"
        :key="entry.ts_ms + '-' + idx"
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
