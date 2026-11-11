<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { operationsApi } from '@/api/operations.ts'
import type { LogEntry } from '@/types'
import Button from '@/components/ui/Button.vue'

const entries = ref<LogEntry[]>([])
const total = ref(0)
const page = ref(1)
const search = ref('')
const requestId = ref('')
const level = ref('INFO')
const loading = ref(false)
const error = ref('')
async function load(reset = false) {
  if (reset) page.value = 1
  loading.value = true
  try {
    const result = await operationsApi.logs(new URLSearchParams({ page: String(page.value), search: search.value, request_id: requestId.value, level: level.value }))
    entries.value = result.items
    total.value = result.total
    error.value = ''
  } catch (err) { error.value = err instanceof Error ? err.message : '历史日志加载失败' }
  finally { loading.value = false }
}
async function turn(delta: number) { page.value += delta; await load() }
onMounted(() => load())
</script>

<template>
  <div class="space-y-3">
    <form
      class="flex flex-wrap gap-2 bg-white border border-slate-200 rounded-xl p-4"
      @submit.prevent="load(true)"
    >
      <input
        v-model="search"
        aria-label="历史日志关键词"
        placeholder="历史日志关键词"
        class="flex-1 border border-slate-200 rounded-lg p-2 text-xs"
      ><input
        v-model="requestId"
        aria-label="请求 ID"
        placeholder="完整 Request ID"
        class="flex-1 border border-slate-200 rounded-lg p-2 text-xs font-mono"
      ><select
        v-model="level"
        aria-label="最低级别"
        class="border border-slate-200 rounded-lg p-2 text-xs"
      >
        <option
          v-for="item in ['DEBUG', 'INFO', 'WARNING', 'ERROR']"
          :key="item"
        >
          {{ item }}
        </option>
      </select><Button
        type="submit"
        :loading="loading"
      >
        查询归档
      </Button>
    </form>
    <p class="text-xs text-slate-500">
      归档随服务重启保留，最多保留 6 个 5MB 日志文件。当前匹配 {{ total }} 条。
    </p>
    <p
      v-if="error"
      role="alert"
      class="text-rose-600 text-sm"
    >
      {{ error }}
    </p>
    <div class="bg-slate-950 text-slate-200 rounded-xl p-4 max-h-[65vh] min-h-80 overflow-auto text-xs font-mono">
      <div
        v-for="(entry, index) in entries"
        :key="index"
        class="py-2 border-b border-slate-800 whitespace-pre-wrap break-all"
      >
        <span class="text-slate-500">{{ entry.ts }}</span> <span :class="entry.level === 'ERROR' ? 'text-rose-400' : 'text-sky-400'">[{{ entry.level }}]</span> {{ entry.message }}<p
          v-if="entry.request_id"
          class="text-slate-500 mt-1"
        >
          {{ entry.request_id }}
        </p>
      </div><p
        v-if="!entries.length"
        class="text-slate-500 py-12 text-center"
      >
        暂无历史日志
      </p>
    </div>
    <div class="flex items-center justify-between text-xs text-slate-500">
      <span>第 {{ page }} / {{ Math.max(1, Math.ceil(total / 100)) }} 页</span><div class="flex gap-2">
        <Button
          :disabled="page <= 1 || loading"
          @click="turn(-1)"
        >
          上一页
        </Button><Button
          :disabled="page * 100 >= total || loading"
          @click="turn(1)"
        >
          下一页
        </Button>
      </div>
    </div>
  </div>
</template>
