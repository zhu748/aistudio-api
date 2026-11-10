<script setup lang="ts">
interface Props {
  variant?: 'green' | 'red' | 'amber' | 'blue' | 'gray' | 'brand'
  size?: 'sm' | 'md'
}

withDefaults(defineProps<Props>(), {
  variant: 'gray',
  size: 'md',
})
</script>

<template>
  <span
    class="inline-flex items-center justify-center font-medium rounded-md tabular-nums transition-colors"
    :class="[
      size === 'sm' ? 'px-1.5 py-0.5 text-[11px]' : 'px-2 py-0.5 text-xs',
      {
        'bg-emerald-50 text-emerald-700 border border-emerald-200/70': variant === 'green',
        'bg-rose-50 text-rose-700 border border-rose-200/70': variant === 'red',
        'bg-amber-50 text-amber-700 border border-amber-200/70': variant === 'amber',
        'bg-sky-50 text-sky-700 border border-sky-200/70': variant === 'blue',
        'bg-brand-50 text-brand-700 border border-brand-200/70': variant === 'brand',
        'bg-slate-100 text-slate-600 border border-slate-200/70': variant === 'gray',
      },
    ]"
  >
    <slot />
  </span>
</template>
