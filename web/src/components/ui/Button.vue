<script setup lang="ts">
import { Loader2 } from 'lucide-vue-next'

interface Props {
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost' | 'outline' | 'brand'
  size?: 'xs' | 'sm' | 'md' | 'lg'
  loading?: boolean
  disabled?: boolean
  type?: 'button' | 'submit' | 'reset'
}

withDefaults(defineProps<Props>(), {
  variant: 'secondary',
  size: 'md',
  loading: false,
  disabled: false,
  type: 'button',
})
</script>

<template>
  <button
    :type="type"
    :disabled="disabled || loading"
    class="inline-flex items-center justify-center font-medium rounded-lg transition-all select-none disabled:opacity-50 disabled:cursor-not-allowed cursor-pointer active:scale-[0.99] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500/30"
    :class="[
      size === 'xs'
        ? 'px-2.5 py-1 text-[11px] gap-1'
        : size === 'sm'
          ? 'px-3 py-1.5 text-xs gap-1.5'
          : size === 'lg'
            ? 'px-5 py-2.5 text-sm gap-2'
            : 'px-3.5 py-2 text-xs gap-1.5',
      {
        'bg-slate-900 hover:bg-slate-800 text-white shadow-2xs': variant === 'primary',
        'bg-brand-600 hover:bg-brand-700 text-white shadow-2xs': variant === 'brand',
        'bg-white hover:bg-slate-50 text-slate-700 border border-slate-200/90 shadow-2xs': variant === 'secondary',
        'bg-rose-50 hover:bg-rose-100/80 text-rose-700 border border-rose-200/80': variant === 'danger',
        'hover:bg-slate-100 text-slate-600 hover:text-slate-900': variant === 'ghost',
        'bg-white border border-slate-200/90 text-slate-700 hover:bg-slate-50 hover:border-slate-300 shadow-2xs': variant === 'outline',
      },
    ]"
  >
    <Loader2
      v-if="loading"
      class="w-3.5 h-3.5 animate-spin shrink-0"
    />
    <slot />
  </button>
</template>
