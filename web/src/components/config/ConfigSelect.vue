<script setup lang="ts">
export interface SelectOption {
  value: string | number | null
  label: string
  description?: string
  badge?: string
}

interface Props {
  modelValue: string | number | null | undefined
  label: string
  description?: string
  options: SelectOption[]
  disabled?: boolean
}

const props = withDefaults(defineProps<Props>(), {
  description: '',
  disabled: false,
})

const emit = defineEmits<{
  'update:modelValue': [val: string | number | null]
}>()

function formatOptionLabel(opt: SelectOption): string {
  if (!opt.description || opt.value === null || opt.label.includes(opt.description)) {
    return opt.label
  }
  return `${opt.label} — ${opt.description}`
}

function handleChange(e: Event) {
  const raw = (e.target as HTMLSelectElement).value
  if (raw === '__null__') {
    emit('update:modelValue', null)
    return
  }
  const match = props.options.find((opt) => String(opt.value) === raw)
  if (match) {
    emit('update:modelValue', match.value)
  } else {
    emit('update:modelValue', raw)
  }
}
</script>

<template>
  <div class="space-y-1.5">
    <div class="flex items-center justify-between">
      <label class="block text-xs font-semibold text-slate-700">
        {{ label }}
      </label>
      <span
        v-if="description"
        class="text-[11px] text-slate-400"
      >
        {{ description }}
      </span>
    </div>

    <div class="relative">
      <select
        :value="modelValue === null || modelValue === undefined ? '__null__' : modelValue"
        :disabled="disabled"
        class="w-full px-3 py-1.5 bg-white border border-slate-200 rounded-lg text-xs font-mono outline-none focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all cursor-pointer disabled:opacity-50"
        @change="handleChange"
      >
        <option
          v-for="opt in options"
          :key="String(opt.value)"
          :value="opt.value === null ? '__null__' : opt.value"
        >
          {{ formatOptionLabel(opt) }}
        </option>
      </select>
    </div>
  </div>
</template>
