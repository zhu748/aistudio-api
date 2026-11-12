<script setup lang="ts">
import { watch, onMounted, onUnmounted, ref, nextTick, useId } from 'vue'
import { X } from 'lucide-vue-next'
import { setModalScroll } from '@/composables/modalScroll.ts'

interface Props {
  modelValue: boolean
  title?: string
  subtitle?: string
  maxWidth?: 'sm' | 'md' | 'lg' | 'xl' | '2xl' | '4xl'
}

const props = withDefaults(defineProps<Props>(), {
  title: '',
  subtitle: '',
  maxWidth: 'md',
})

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
  close: []
}>()
const titleId = useId()
const panel = ref<HTMLElement | null>(null)
let previousFocus: HTMLElement | null = null

function closeModal() {
  emit('update:modelValue', false)
  emit('close')
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && props.modelValue) {
    closeModal()
  }
  if (e.key === 'Tab' && props.modelValue && panel.value) {
    const nodes = [
      ...panel.value.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]',
      ),
    ].filter((node) => node.getClientRects().length)
    const first = nodes[0]
    const last = nodes[nodes.length - 1]
    if (e.shiftKey && (document.activeElement === first || document.activeElement === panel.value)) {
      e.preventDefault()
      last?.focus()
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault()
      first?.focus()
    }
  }
}

onMounted(() => {
  window.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', handleKeydown)
  setModalScroll(titleId, false)
})

watch(
  () => props.modelValue,
  (val) => {
    setModalScroll(titleId, val)
    if (val) {
      previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null
      void nextTick(() => panel.value?.focus())
    } else {
      if (previousFocus?.isConnected) previousFocus.focus()
    }
  },
  { immediate: true },
)

const widthClasses: Record<string, string> = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-lg',
  xl: 'max-w-xl',
  '2xl': 'max-w-2xl',
  '4xl': 'max-w-4xl',
}
</script>

<template>
  <Teleport to="body">
    <Transition
      enter-active-class="transition duration-150 ease-out"
      enter-from-class="opacity-0"
      enter-to-class="opacity-100"
      leave-active-class="transition duration-100 ease-in"
      leave-from-class="opacity-100"
      leave-to-class="opacity-0"
    >
      <div
        v-if="modelValue"
        class="fixed inset-0 z-50 flex items-center justify-center p-4 bg-slate-900/40 backdrop-blur-xs overflow-y-auto"
        @click.self="closeModal"
      >
        <Transition
          enter-active-class="transition duration-150 ease-out"
          enter-from-class="opacity-0 scale-95 translate-y-1"
          enter-to-class="opacity-100 scale-100 translate-y-0"
          leave-active-class="transition duration-100 ease-in"
          leave-from-class="opacity-100 scale-100 translate-y-0"
          leave-to-class="opacity-0 scale-95 translate-y-1"
        >
          <div
            v-if="modelValue"
            ref="panel"
            role="dialog"
            aria-modal="true"
            :aria-labelledby="titleId"
            tabindex="-1"
            :class="[
              'w-full bg-white rounded-xl shadow-2xl border border-slate-200/90 flex flex-col overflow-hidden my-4 max-h-[94dvh] outline-none',
              widthClasses[maxWidth] || 'max-w-md',
            ]"
          >
            <!-- Modal Header -->
            <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between gap-3 shrink-0">
              <div class="min-w-0">
                <h3
                  :id="titleId"
                  class="text-sm font-semibold text-slate-900 tracking-tight"
                >
                  {{ title }}
                </h3>
                <p
                  v-if="subtitle"
                  class="text-xs text-slate-500 mt-0.5 break-all"
                >
                  {{ subtitle }}
                </p>
              </div>
              <button
                type="button"
                aria-label="关闭对话框"
                class="text-slate-400 hover:text-slate-600 p-1 rounded-lg hover:bg-slate-100 transition-colors cursor-pointer"
                @click="closeModal"
              >
                <X class="w-4 h-4" />
              </button>
            </div>

            <!-- Modal Body -->
            <div class="px-5 py-4 overflow-y-auto min-h-0">
              <slot />
            </div>

            <!-- Modal Footer -->
            <div
              v-if="$slots.footer"
              class="px-5 py-3 bg-slate-50/80 border-t border-slate-100 flex items-center justify-end gap-2.5"
            >
              <slot name="footer" />
            </div>
          </div>
        </Transition>
      </div>
    </Transition>
  </Teleport>
</template>
