<script setup lang="ts">
import { watch, onMounted, onUnmounted } from 'vue'
import { X } from 'lucide-vue-next'

interface Props {
  modelValue: boolean
  title?: string
  subtitle?: string
  maxWidth?: 'sm' | 'md' | 'lg' | 'xl' | '2xl'
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

function closeModal() {
  emit('update:modelValue', false)
  emit('close')
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && props.modelValue) {
    closeModal()
  }
}

onMounted(() => {
  window.addEventListener('keydown', handleKeydown)
})

onUnmounted(() => {
  window.removeEventListener('keydown', handleKeydown)
})

watch(
  () => props.modelValue,
  (val) => {
    if (val) {
      document.body.style.overflow = 'hidden'
    } else {
      document.body.style.overflow = ''
    }
  }
)

const widthClasses: Record<string, string> = {
  sm: 'max-w-sm',
  md: 'max-w-md',
  lg: 'max-w-lg',
  xl: 'max-w-xl',
  '2xl': 'max-w-2xl',
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
            :class="[
              'w-full bg-white rounded-xl shadow-2xl border border-slate-200/90 flex flex-col overflow-hidden my-8',
              widthClasses[maxWidth] || 'max-w-md',
            ]"
          >
            <!-- Modal Header -->
            <div class="px-5 py-4 border-b border-slate-100 flex items-center justify-between gap-3">
              <div>
                <h3 class="text-sm font-semibold text-slate-900 tracking-tight">
                  {{ title }}
                </h3>
                <p
                  v-if="subtitle"
                  class="text-[11px] text-slate-500 mt-0.5"
                >
                  {{ subtitle }}
                </p>
              </div>
              <button
                type="button"
                class="text-slate-400 hover:text-slate-600 p-1 rounded-lg hover:bg-slate-100 transition-colors cursor-pointer"
                @click="closeModal"
              >
                <X class="w-4 h-4" />
              </button>
            </div>

            <!-- Modal Body -->
            <div class="px-5 py-4 overflow-y-auto max-h-[75vh]">
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
