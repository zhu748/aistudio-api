<script setup lang="ts">
import type { Component } from 'vue'

export interface SegmentedOption {
  value: string
  label: string
  /** 数字角标：countStyle='paren' 显示为「标签 (N)」，'muted' 显示为淡色小字 */
  count?: number
  /** 可选前置图标（lucide 组件） */
  icon?: Component
  /** 图标 class，默认 w-3.5 h-3.5 */
  iconClass?: string
  /** 该选项的选中态 class（覆盖组件级 activeClass） */
  activeClass?: string
  /** 该选项的未选中态 class（覆盖组件级 idleClass） */
  idleClass?: string
  /** 该选项按钮的基础 class（覆盖组件级 itemClass） */
  itemClass?: string
}

interface Props {
  options: SegmentedOption[]
  modelValue: string
  variant?: 'light' | 'dark'
  size?: 'xs' | 'sm'
  /** 覆盖整体容器 class（默认按 variant 取内置样式） */
  containerClass?: string
  /** 覆盖按钮基础 class（默认按 size 取内置样式） */
  itemClass?: string
  /** 覆盖选中态 class（浅色变体默认白底深字） */
  activeClass?: string
  /** 覆盖未选中态 class（默认灰字，hover 变深） */
  idleClass?: string
  countStyle?: 'paren' | 'muted'
}

const props = withDefaults(defineProps<Props>(), {
  variant: 'light',
  size: 'sm',
  containerClass: '',
  itemClass: '',
  activeClass: '',
  idleClass: '',
  countStyle: 'paren',
})

const emit = defineEmits<{
  'update:modelValue': [value: string]
}>()

const defaultContainer = () =>
  props.variant === 'dark'
    ? 'flex items-center gap-1.5 flex-wrap'
    : 'flex items-center rounded-lg bg-slate-100 p-0.5 text-xs font-medium'
const defaultItem = () =>
  props.size === 'xs'
    ? 'px-2 py-0.5 rounded-md text-[11px] transition-all cursor-pointer'
    : 'px-2.5 py-1 rounded-md transition-all cursor-pointer'
const defaultActive = () =>
  props.variant === 'dark'
    ? 'bg-slate-900 text-white shadow-2xs'
    : 'bg-white text-slate-900 font-semibold shadow-2xs'
const defaultIdle = () =>
  props.variant === 'dark'
    ? 'text-slate-600 hover:bg-slate-100 bg-slate-50 border border-slate-200/70'
    : 'text-slate-500 hover:text-slate-900'
</script>

<template>
  <div :class="containerClass || defaultContainer()">
    <button
      v-for="opt in options"
      :key="opt.value"
      type="button"
      :class="[
        opt.itemClass || itemClass || defaultItem(),
        modelValue === opt.value
          ? opt.activeClass || activeClass || defaultActive()
          : opt.idleClass || idleClass || defaultIdle(),
      ]"
      @click="emit('update:modelValue', opt.value)"
    >
      <component
        :is="opt.icon"
        v-if="opt.icon"
        :class="opt.iconClass || 'w-3.5 h-3.5'"
      />
      <span>{{ opt.label }}<template v-if="opt.count !== undefined && countStyle === 'paren'"> ({{ opt.count }})</template></span>
      <span
        v-if="opt.count !== undefined && countStyle === 'muted'"
        class="ml-1 font-mono text-[10px] opacity-70"
      >{{ opt.count }}</span>
    </button>
  </div>
</template>
