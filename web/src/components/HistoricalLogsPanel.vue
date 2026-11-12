<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue'
import { operationsApi } from '@/api/operations.ts'
import type { LogEntry } from '@/types'
import Button from '@/components/ui/Button.vue'
import { usePageRefresh } from '@/composables/usePageRefresh.ts'

const entries = ref<LogEntry[]>([])
const props = defineProps<{ category: string; requestId: string; model: string; accountId: string }>()
const total = ref(0)
const page = ref(1)
const search = ref('')
const level = ref('INFO')
const loading = ref(false)
const error = ref('')
let loadSequence = 0
async function load(reset = false) {
  const current = ++loadSequence
  if (reset) page.value = 1
  loading.value = true
  try {
    const result = await operationsApi.logs(
      new URLSearchParams({
        page: String(page.value),
        search: search.value,
        request_id: props.requestId.trim(),
        category: props.category,
        model: props.model.trim(),
        account_id: props.accountId.trim(),
        level: level.value,
      }),
    )
    if (current !== loadSequence) return false
    entries.value = result.items
    total.value = result.total
    error.value = ''
    return true
  } catch (err) {
    if (current === loadSequence) error.value = err instanceof Error ? err.message : '历史日志加载失败'
    return false
  } finally {
    if (current === loadSequence) loading.value = false
  }
}
async function turn(delta: number) {
  page.value += delta
  await load()
}
let sequence = 0
usePageRefresh(load)
watch(
  () => [props.category, props.requestId, props.model, props.accountId],
  () => {
    const current = ++sequence
    loadSequence++
    entries.value = []
    setTimeout(() => {
      if (current === sequence) void load(true)
    }, 300)
  },
  { immediate: true },
)
onUnmounted(() => {
  sequence++
  loadSequence++
})
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
    <div
      class="bg-slate-950 text-slate-200 rounded-xl p-4 max-h-[65vh] min-h-80 overflow-auto text-xs font-mono"
    >
      <div
        v-for="(entry, index) in entries"
        :key="index"
        class="py-2 border-b border-slate-800 whitespace-pre-wrap break-all"
      >
        <span class="text-slate-500">{{ entry.ts }}</span>
        <span :class="entry.level === 'ERROR' ? 'text-rose-400' : 'text-sky-400'">[{{ entry.level }}]</span>
        <span class="text-brand-300">{{ entry.model }}</span><span class="ml-2 text-slate-500">{{
          entry.account_id
        }}</span>
        <p class="mt-1 leading-6 text-[13px]">
          {{ entry.message }}
        </p>
        <p
          v-if="entry.request_id"
          class="text-slate-500 mt-1"
        >
          <router-link
            :to="{ path: '/requests', query: { request_id: entry.request_id } }"
            class="text-sky-400 hover:underline"
          >
            {{ entry.request_id }} ↗
          </router-link>
        </p>
      </div>
      <p
        v-if="!entries.length"
        class="text-slate-500 py-12 text-center"
      >
        暂无历史日志
      </p>
    </div>
    <div class="flex items-center justify-between text-xs text-slate-500">
      <span>第 {{ page }} / {{ Math.max(1, Math.ceil(total / 100)) }} 页</span>
      <div class="flex gap-2">
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
