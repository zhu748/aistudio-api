<script setup lang="ts">
import { ref, useId, nextTick, onMounted, onUnmounted } from 'vue'
import { HelpCircle, CheckCircle2 } from 'lucide-vue-next'
import { CONFIG_SCHEMA } from './schema.ts'

const props = withDefaults(
  defineProps<{
    schemaKey?: string
    title?: string
    content?: string
    explanation?: string
    recommended?: string | number | boolean
  }>(),
  {
    schemaKey: undefined,
    title: undefined,
    content: undefined,
    explanation: undefined,
    // Explicitly default to undefined so Vue does not coerce missing boolean union prop to false
    recommended: undefined,
  }
)

const show = ref(false)
const pinned = ref(false)
const triggerRef = ref<HTMLElement | null>(null)
const tooltipRef = ref<HTMLElement | null>(null)
const tooltipId = useId()
const boxStyle = ref<{ top: string; left: string }>({ top: '0px', left: '0px' })
let hideTimer: ReturnType<typeof setTimeout> | null = null

const fieldDef = props.schemaKey ? CONFIG_SCHEMA[props.schemaKey] : null
const displayTitle = props.title || fieldDef?.title || ''
const displayContent = props.content || fieldDef?.description || ''
const displayExplanation = props.explanation || fieldDef?.explanation || ''
const displayRecommended = props.recommended !== undefined ? props.recommended : fieldDef?.recommendedValue

function updatePosition() {
  const el = triggerRef.value
  if (!el || typeof window === 'undefined') return
  const rect = el.getBoundingClientRect()
  const vw = window.innerWidth
  const vh = window.innerHeight
  const boxWidth = tooltipRef.value?.offsetWidth ?? Math.min(vw < 640 ? 288 : 320, vw - 24)
  const boxHeight = tooltipRef.value?.offsetHeight ?? 0
  const rawLeft = rect.left + rect.width / 2 - boxWidth / 2
  const clampedLeft = Math.max(12, Math.min(rawLeft, vw - boxWidth - 12))
  const placeBelow = vh - rect.bottom >= boxHeight + 20 || rect.top < boxHeight + 20
  const rawTop = placeBelow ? rect.bottom + 8 : rect.top - boxHeight - 8
  boxStyle.value = {
    left: `${Math.round(clampedLeft)}px`,
    top: `${Math.round(Math.max(12, Math.min(rawTop, vh - boxHeight - 12)))}px`,
  }
}

function closeTip() {
  pinned.value = false
  show.value = false
}

function handleViewportChange() {
  if (show.value) updatePosition()
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') closeTip()
}

function openTip() {
  if (hideTimer) {
    clearTimeout(hideTimer)
    hideTimer = null
  }
  updatePosition()
  show.value = true
  void nextTick(updatePosition)
}

function scheduleHide() {
  if (pinned.value) return
  if (hideTimer) clearTimeout(hideTimer)
  hideTimer = setTimeout(() => {
    if (!pinned.value) {
      show.value = false
    }
  }, 100)
}

function toggleTip() {
  if (show.value && pinned.value) {
    closeTip()
  } else {
    pinned.value = true
    openTip()
  }
}

function handleDocumentClick(e: MouseEvent) {
  if (!show.value) return
  const target = e.target as Node | null
  if (
    target &&
    !triggerRef.value?.contains(target) &&
    !tooltipRef.value?.contains(target)
  ) {
    closeTip()
  }
}

onMounted(() => {
  document.addEventListener('click', handleDocumentClick)
  document.addEventListener('keydown', handleKeydown)
  window.addEventListener('scroll', handleViewportChange, true)
  window.addEventListener('resize', handleViewportChange)
})

onUnmounted(() => {
  if (hideTimer) clearTimeout(hideTimer)
  document.removeEventListener('click', handleDocumentClick)
  document.removeEventListener('keydown', handleKeydown)
  window.removeEventListener('scroll', handleViewportChange, true)
  window.removeEventListener('resize', handleViewportChange)
})
</script>

<template>
  <div
    ref="triggerRef"
    class="relative inline-flex items-center"
    @mouseenter="openTip"
    @mouseleave="scheduleHide"
  >
    <button
      type="button"
      class="text-slate-400 hover:text-brand-600 transition-colors p-0.5 rounded cursor-pointer"
      :class="{ 'text-brand-600': show && pinned }"
      :title="displayTitle"
      :aria-label="displayTitle"
      :aria-describedby="show ? tooltipId : undefined"
      :aria-expanded="show"
      @focus="openTip"
      @blur="scheduleHide"
      @click.stop="toggleTip"
    >
      <HelpCircle class="w-3.5 h-3.5" />
    </button>

    <!-- Viewport-clamped Floating Tooltip Box -->
    <Teleport to="body">
      <Transition
        enter-active-class="transition duration-150 ease-out"
        enter-from-class="opacity-0 scale-95"
        enter-to-class="opacity-100 scale-100"
        leave-active-class="transition duration-100 ease-in"
        leave-from-class="opacity-100 scale-100"
        leave-to-class="opacity-0 scale-95"
      >
        <div
          v-if="show"
          :id="tooltipId"
          ref="tooltipRef"
          role="tooltip"
          class="fixed z-50 w-72 sm:w-80 max-w-[calc(100vw-24px)] max-h-[calc(100dvh-24px)] overflow-y-auto break-words bg-slate-900 text-slate-100 p-3.5 rounded-xl shadow-2xl border border-slate-700 text-xs space-y-2 pointer-events-auto"
          :style="boxStyle"
          @mouseenter="openTip"
          @mouseleave="scheduleHide"
        >
          <div class="flex items-center justify-between gap-2 border-b border-slate-800 pb-1.5 font-bold text-brand-300">
            <span>{{ displayTitle }}</span>
            <span
              v-if="fieldDef?.tags?.length"
              class="text-[10px] font-normal px-1.5 py-0.5 rounded bg-slate-800 text-slate-400 shrink-0"
            >
              {{ fieldDef.tags.join(' · ') }}
            </span>
          </div>

          <div class="text-slate-300 leading-relaxed text-[11px]">
            {{ displayContent }}
          </div>

          <div
            v-if="displayExplanation"
            class="text-slate-400 text-[11px] leading-relaxed pt-1 border-t border-slate-800/80"
          >
            <strong class="text-slate-300">业务原理:</strong> {{ displayExplanation }}
          </div>

          <div
            v-if="displayRecommended !== undefined"
            class="flex items-center gap-1.5 text-[11px] text-emerald-400 font-semibold bg-emerald-950/60 px-2 py-1 rounded-lg border border-emerald-800/60"
          >
            <CheckCircle2 class="w-3.5 h-3.5 shrink-0 text-emerald-400" />
            <span>推荐配置: {{ String(displayRecommended) }}</span>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>
