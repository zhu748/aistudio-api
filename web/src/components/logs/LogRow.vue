<script setup lang="ts">
import ThoughtSignatureBadges from '@/components/ThoughtSignatureBadges.vue'
import type { LogEntry } from '@/types'
import { stageLabels } from '@/utils/requests.ts'
import { requestLogRoute, SHORT_REQUEST_ID } from '@/utils/thoughtSignature.ts'

withDefaults(
  defineProps<{
    entry: LogEntry
    /** 是否渲染跳转到请求详情的链接 */
    linkToRequest?: boolean
    /** 请求详情里的极简单行渲染（无徽章、无链接） */
    compact?: boolean
    /** inline：运行日志列表行；archive：历史归档行 */
    variant?: 'inline' | 'archive'
  }>(),
  { linkToRequest: true, compact: false, variant: 'inline' },
)

const emit = defineEmits<{
  filterFp: [fp: string]
}>()

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

/** 纯文本级别配色（历史归档行使用，非 ERROR/WARNING 一律蓝色） */
const levelTextClass: Record<string, string> = {
  ERROR: 'text-rose-400',
  WARNING: 'text-amber-400',
}
</script>

<template>
  <p
    v-if="compact"
    class="py-2 border-b border-slate-800"
  >
    <span class="text-slate-500">{{ entry.ts }}</span><span :class="entry.level === 'ERROR' ? 'text-rose-400' : 'text-sky-400'">[{{ entry.level }}]</span> {{ entry.message }}
  </p>
  <div
    v-else-if="variant === 'archive'"
    class="py-2.5 border-b border-slate-800 break-words"
  >
    <div class="flex flex-wrap items-center gap-x-2 gap-y-0.5">
      <span class="text-slate-500">{{ entry.ts }}</span>
      <span :class="levelTextClass[entry.level] || 'text-sky-400'">[{{ entry.level }}]</span>
      <span
        v-if="entry.model"
        class="text-brand-300"
      >{{ entry.model }}</span>
      <span
        v-if="entry.account_id"
        class="text-slate-500"
      >{{ entry.account_id }}</span>
    </div>
    <ThoughtSignatureBadges
      v-if="entry.sig_meta"
      :meta="entry.sig_meta"
      @filter-fp="emit('filterFp', $event)"
    />
    <p class="mt-1 leading-6 text-[13px] whitespace-pre-wrap break-words">
      {{ entry.message }}
    </p>
    <p
      v-if="entry.request_id"
      class="text-slate-500 mt-1"
    >
      <router-link
        :to="requestLogRoute(entry)"
        class="text-sky-400 hover:underline"
      >
        {{ entry.request_id }} ↗
      </router-link>
    </p>
  </div>
  <div
    v-else
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
        v-if="linkToRequest && entry.request_id && entry.category === 'model'"
        :to="requestLogRoute(entry)"
        class="text-sky-400 hover:underline text-xs font-mono"
        :title="entry.request_id"
      >
        {{ entry.request_id.slice(0, SHORT_REQUEST_ID) }}… ↗
      </router-link>
      <div
        v-if="entry.sig_meta"
        class="w-full"
      >
        <ThoughtSignatureBadges
          :meta="entry.sig_meta"
          @filter-fp="emit('filterFp', $event)"
        />
      </div>
      <span class="text-slate-200 whitespace-pre-wrap break-words w-full leading-6 text-[13px]">
        {{ entry.message }}
      </span>
    </div>
  </div>
</template>
