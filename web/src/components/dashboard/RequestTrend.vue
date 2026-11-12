<script setup lang="ts">
import { computed } from 'vue'
import type { RequestMetricsResponse } from '@/types/stats.ts'
const props = defineProps<{ stats: RequestMetricsResponse }>()
const maximum = computed(() => Math.max(1, ...props.stats.series.map((item) => item.requests)))
const step = computed(() => 680 / Math.max(1, props.stats.series.length))
function label(timestamp: number) {
  return new Date(timestamp * 1000).toLocaleString(
    'zh-CN',
    props.stats.interval_seconds === 86400
      ? { month: '2-digit', day: '2-digit' }
      : { hour: '2-digit', minute: '2-digit' },
  )
}
</script>
<template>
  <section class="surface-card rounded-xl p-5">
    <div class="flex flex-wrap items-center justify-between gap-2 mb-4">
      <h2 class="font-semibold text-sm">
        请求趋势
      </h2>
      <p class="text-xs text-slate-500">
        <span class="text-brand-700">● 成功</span><span class="text-rose-600 ml-3">● 未成功</span><span class="ml-3">{{ stats.interval_seconds === 86400 ? '按日' : '按小时' }} · 每次调用计一次</span>
      </p>
    </div>
    <svg
      viewBox="0 0 720 190"
      class="w-full max-h-60"
      role="img"
      aria-label="所选时间范围内每个时段的成功与未成功请求数"
    >
      <line
        v-for="tick in [0, 1, 2, 3]"
        :key="tick"
        x1="30"
        x2="710"
        :y1="15 + tick * 48"
        :y2="15 + tick * 48"
        stroke="#e2e8f0"
        stroke-dasharray="3 4"
      />
      <text
        x="25"
        y="16"
        text-anchor="end"
        font-size="10"
        fill="#64748b"
      >
        {{ maximum }}
      </text>
      <g
        v-for="(item, index) in stats.series"
        :key="item.timestamp"
      >
        <title>
          {{ new Date(item.timestamp * 1000).toLocaleString() }} · {{ item.requests }} 次调用，成功
          {{ item.success }}，未成功 {{ item.requests - item.success }}，Token {{ item.total_tokens }}
        </title>
        <rect
          :x="30 + index * step + 1"
          :y="159 - (item.requests / maximum) * 144"
          :width="Math.max(1, step - 3)"
          :height="(item.requests / maximum) * 144"
          rx="2"
          fill="#e11d48"
          opacity="0.8"
        />
        <rect
          :x="30 + index * step + 1"
          :y="159 - (item.success / maximum) * 144"
          :width="Math.max(1, step - 3)"
          :height="(item.success / maximum) * 144"
          rx="2"
          fill="#0f766e"
        />
        <text
          v-if="index % Math.max(1, Math.ceil(stats.series.length / 6)) === 0"
          :x="30 + index * step"
          y="181"
          font-size="10"
          fill="#64748b"
        >
          {{ label(item.timestamp) }}
        </text>
      </g>
    </svg>
    <p
      v-if="!stats.totals.requests"
      class="text-center text-sm text-slate-500"
    >
      当前时间范围暂无已完成调用。
    </p>
  </section>
</template>
