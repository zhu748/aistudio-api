<script setup lang="ts">
interface Props {
  label: string
  value: string | number
  valueTitle?: string
  subValue?: string
  color?: 'default' | 'green' | 'amber' | 'red' | 'blue'
  mono?: boolean
  compactValue?: boolean
}

withDefaults(defineProps<Props>(), {
  valueTitle: '',
  subValue: '',
  color: 'default',
  mono: true,
  compactValue: false,
})
</script>

<template>
  <div class="bg-white border border-slate-200/90 rounded-xl p-4 shadow-2xs transition-all hover:border-slate-300/90 flex flex-col justify-between gap-2.5 min-w-0">
    <div class="flex items-center justify-between gap-2 text-[11px] font-semibold text-slate-500 uppercase tracking-wider">
      <span class="truncate">{{ label }}</span>
      <div class="flex items-center gap-1.5 shrink-0">
        <slot name="action" />
        <slot name="icon" />
      </div>
    </div>

    <div class="space-y-1 min-w-0">
      <div class="flex items-baseline justify-between gap-x-2 gap-y-0.5 flex-wrap min-w-0">
        <div
          :title="valueTitle || String(value)"
          class="font-bold tracking-tight min-w-0 max-w-full"
          :class="[
            compactValue ? 'text-base sm:text-[17px] leading-snug truncate' : 'text-xl leading-tight break-all',
            mono ? 'font-mono tabular-nums' : '',
            {
              'text-slate-900': color === 'default',
              'text-emerald-600': color === 'green',
              'text-amber-600': color === 'amber',
              'text-rose-600': color === 'red',
              'text-sky-600': color === 'blue',
            },
          ]"
        >
          {{ value }}
        </div>
        <span
          v-if="subValue"
          class="text-[11px] text-slate-400 font-medium font-mono tabular-nums whitespace-nowrap"
        >{{ subValue }}</span>
      </div>

      <div v-if="$slots.footer">
        <slot name="footer" />
      </div>
    </div>
  </div>
</template>
