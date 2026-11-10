<script setup lang="ts">
import type { SafetySettingsMap } from './types.ts'
import { Shield, ShieldAlert, ShieldCheck } from 'lucide-vue-next'

const props = withDefaults(
  defineProps<{
    modelValue?: SafetySettingsMap
    disabled?: boolean
  }>(),
  {
    modelValue: () => ({}),
    disabled: false,
  }
)

const emit = defineEmits<{
  'update:modelValue': [val: SafetySettingsMap]
}>()

const categories = [
  { key: 'Harassment', label: '骚扰内容', desc: '负面或恶意言语攻击' },
  { key: 'Hate', label: '仇恨言论', desc: '针对特定群体的仇恨偏见' },
  { key: 'Sexually Explicit', label: '色情内容', desc: '性暗示或露骨色情' },
  { key: 'Dangerous Content', label: '危险内容', desc: '促进危险行为或暴力' },
]

const levels = [
  { val: 1, label: '最严', desc: '最强过滤强度', color: 'border-rose-300 text-rose-700 bg-rose-50' },
  { val: 2, label: '较严', desc: '较高过滤强度', color: 'border-amber-300 text-amber-700 bg-amber-50' },
  { val: 3, label: '中等', desc: '标准中等强度', color: 'border-sky-300 text-sky-700 bg-sky-50' },
  { val: 4, label: '宽松', desc: '宽松低强度', color: 'border-emerald-300 text-emerald-700 bg-emerald-50' },
  { val: 5, label: '关闭', desc: '完全不拦截', color: 'border-slate-400 text-slate-900 bg-slate-100 font-bold' },
]

function getLevel(catKey: string): number {
  return props.modelValue?.[catKey] ?? 5
}

function setLevel(catKey: string, val: number) {
  if (props.disabled) return
  const next: SafetySettingsMap = { ...(props.modelValue || {}) }
  next[catKey] = val
  emit('update:modelValue', next)
}

function setAll(val: number) {
  if (props.disabled) return
  const next: SafetySettingsMap = {}
  for (const cat of categories) {
    next[cat.key] = val
  }
  emit('update:modelValue', next)
}
</script>

<template>
  <div class="space-y-3 p-3.5 bg-slate-50/70 border border-slate-200/80 rounded-xl">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-2 border-b border-slate-200/60 pb-2">
      <div class="flex items-center gap-2">
        <Shield class="w-4 h-4 text-brand-600" />
        <h4 class="text-xs font-bold text-slate-900">
          安全审查阈值 (Safety Settings)
        </h4>
      </div>

      <!-- Batch Actions -->
      <div
        v-if="!disabled"
        class="flex items-center gap-1.5"
      >
        <button
          type="button"
          class="px-2 py-1 text-[11px] font-medium bg-white text-emerald-700 border border-emerald-200 rounded-md hover:bg-emerald-50 transition-colors flex items-center gap-1 cursor-pointer"
          @click="setAll(5)"
        >
          <ShieldAlert class="w-3 h-3 text-emerald-600" />
          <span>全部关闭拦截 (5)</span>
        </button>
        <button
          type="button"
          class="px-2 py-1 text-[11px] font-medium bg-white text-slate-600 border border-slate-200 rounded-md hover:bg-slate-100 transition-colors flex items-center gap-1 cursor-pointer"
          @click="setAll(3)"
        >
          <ShieldCheck class="w-3 h-3 text-slate-500" />
          <span>全部设为中等 (3)</span>
        </button>
      </div>
    </div>

    <!-- 4 Categories Grid -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-2.5 pt-0.5">
      <div
        v-for="cat in categories"
        :key="cat.key"
        class="p-2.5 bg-white border border-slate-200/70 rounded-lg space-y-1.5"
      >
        <div class="flex items-center justify-between">
          <div>
            <span class="text-xs font-semibold text-slate-800">{{ cat.label }}</span>
            <span class="text-[11px] text-slate-400 ml-1.5">{{ cat.desc }}</span>
          </div>
          <span
            class="text-[10px] font-mono font-semibold px-1.5 py-0.5 rounded tabular-nums"
            :class="getLevel(cat.key) === 5 ? 'bg-emerald-50 text-emerald-700' : 'bg-amber-50 text-amber-800'"
          >
            {{ getLevel(cat.key) === 5 ? '关闭 (5)' : `等级 ${getLevel(cat.key)}` }}
          </span>
        </div>

        <div class="grid grid-cols-5 gap-1 pt-0.5">
          <button
            v-for="lvl in levels"
            :key="lvl.val"
            type="button"
            :disabled="disabled"
            class="py-1 px-1 text-center rounded border text-[11px] font-mono transition-all cursor-pointer select-none disabled:opacity-50"
            :class="[
              getLevel(cat.key) === lvl.val
                ? `${lvl.color} ring-1 ring-brand-500/20 font-semibold`
                : 'bg-slate-50/80 text-slate-600 border-slate-200/80 hover:bg-slate-100',
            ]"
            :title="lvl.desc"
            @click="setLevel(cat.key, lvl.val)"
          >
            {{ lvl.val }} · {{ lvl.label }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>
