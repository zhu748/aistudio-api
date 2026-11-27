<script setup lang="ts">
import { ref, watch, onUnmounted } from 'vue'
import { operationsApi } from '@/api/operations.ts'
import type { LogEntry } from '@/types'
import Button from '@/components/ui/Button.vue'
import LogRow from '@/components/logs/LogRow.vue'
import { usePageRefresh } from '@/composables/usePageRefresh.ts'
import { useLatest } from '@/composables/useLatest.ts'
import { useDebouncedWatch } from '@/composables/useDebouncedWatch.ts'
import { errorMessage } from '@/utils/errors.ts'

const entries = ref<LogEntry[]>([])
const props = defineProps<{
  category: string
  requestId: string
  model: string
  accountId: string
  initialSearch?: string
}>()
const total = ref(0)
const page = ref(1)
const search = ref(props.initialSearch || '')
const level = ref('INFO')
const loading = ref(false)
const error = ref('')
const loadLatest = useLatest()
async function load(reset = false) {
  const isLatest = loadLatest.begin()
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
    if (!isLatest()) return false
    entries.value = result.items
    total.value = result.total
    error.value = ''
    return true
  } catch (err) {
    if (isLatest()) error.value = errorMessage(err, '历史日志加载失败')
    return false
  } finally {
    if (isLatest()) loading.value = false
  }
}
async function turn(delta: number) {
  page.value += delta
  await load()
}
function filterByFp(fp: string) {
  search.value = fp
  void load(true)
}
usePageRefresh(load)
watch(
  () => props.initialSearch,
  (val) => {
    if (typeof val === 'string' && val !== search.value) {
      search.value = val
      void load(true)
    }
  },
)
useDebouncedWatch(
  () => [props.category, props.requestId, props.model, props.accountId],
  () => void load(true),
  {
    immediate: true,
    onChange: () => {
      loadLatest.invalidate()
      entries.value = []
    },
  },
)
onUnmounted(() => {
  loadLatest.invalidate()
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
      <LogRow
        v-for="entry in entries"
        :key="`${entry.ts_ms}-${entry.message}`"
        :entry="entry"
        variant="archive"
        @filter-fp="filterByFp"
      />
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
