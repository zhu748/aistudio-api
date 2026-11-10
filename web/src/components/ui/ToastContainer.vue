<script setup lang="ts">
import { useToastStore } from '@/stores/toast.ts'
import { CheckCircle2, AlertCircle, AlertTriangle, Info, X } from 'lucide-vue-next'

const toastStore = useToastStore()
</script>

<template>
  <div class="fixed top-4 right-4 z-50 flex flex-col gap-2 max-w-sm w-full pointer-events-none px-4 sm:px-0">
    <TransitionGroup
      enter-active-class="transition duration-200 ease-out"
      enter-from-class="opacity-0 translate-y-[-8px] scale-95"
      enter-to-class="opacity-100 translate-y-0 scale-100"
      leave-active-class="transition duration-150 ease-in"
      leave-from-class="opacity-100 translate-y-0 scale-100"
      leave-to-class="opacity-0 translate-y-[-8px] scale-95"
    >
      <div
        v-for="toast in toastStore.toasts"
        :key="toast.id"
        class="pointer-events-auto flex items-start gap-2.5 p-3 rounded-xl shadow-lg border bg-white/95 backdrop-blur-md text-xs font-medium transition-all"
        :class="{
          'border-emerald-200/90 text-slate-800': toast.type === 'success',
          'border-rose-200/90 text-slate-800': toast.type === 'error',
          'border-amber-200/90 text-slate-800': toast.type === 'warning',
          'border-sky-200/90 text-slate-800': toast.type === 'info',
        }"
      >
        <CheckCircle2
          v-if="toast.type === 'success'"
          class="w-4 h-4 text-emerald-500 shrink-0 mt-0.5"
        />
        <AlertCircle
          v-else-if="toast.type === 'error'"
          class="w-4 h-4 text-rose-500 shrink-0 mt-0.5"
        />
        <AlertTriangle
          v-else-if="toast.type === 'warning'"
          class="w-4 h-4 text-amber-500 shrink-0 mt-0.5"
        />
        <Info
          v-else
          class="w-4 h-4 text-sky-500 shrink-0 mt-0.5"
        />

        <div class="flex-1 leading-relaxed break-words pt-px">
          {{ toast.message }}
        </div>

        <button
          type="button"
          class="text-slate-400 hover:text-slate-600 rounded-md p-0.5 transition-colors cursor-pointer"
          @click="toastStore.remove(toast.id)"
        >
          <X class="w-3.5 h-3.5" />
        </button>
      </div>
    </TransitionGroup>
  </div>
</template>
