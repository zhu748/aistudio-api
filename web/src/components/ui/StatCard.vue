<script setup lang="ts">
interface Props {
  label: string
  value: string | number
  subValue?: string
  color?: 'default' | 'green' | 'amber' | 'red' | 'blue'
  mono?: boolean
}

withDefaults(defineProps<Props>(), {
  subValue: '',
  color: 'default',
  mono: true,
})
</script>

<template>
  <div class="bg-white border border-slate-200/90 rounded-xl p-4 shadow-2xs transition-all hover:border-slate-300/90 flex flex-col justify-between gap-2.5">
    <div class="flex items-center justify-between text-[11px] font-semibold text-slate-500 uppercase tracking-wider">
      <span>{{ label }}</span>
      <div class="flex items-center gap-1.5">
        <slot name="action" />
        <slot name="icon" />
      </div>
    </div>

    <div class="space-y-1">
      <div class="flex items-baseline justify-between gap-2">
        <div
          class="text-xl font-bold tracking-tight truncate max-w-full"
          :class="[
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
          class="text-[11px] text-slate-400 font-medium font-mono tabular-nums shrink-0"
        >{{ subValue }}</span>
      </div>

      <div v-if="$slots.footer">
        <slot name="footer" />
      </div>
    </div>
  </div>
</template>
