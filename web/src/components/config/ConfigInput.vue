<script setup lang="ts">
import FieldHelpTip from './FieldHelpTip.vue'

interface Props {
  modelValue: string | number | null | undefined
  label: string
  description?: string
  placeholder?: string
  type?: 'text' | 'number'
  disabled?: boolean
  schemaKey?: string
}

withDefaults(defineProps<Props>(), {
  description: '',
  placeholder: '',
  type: 'text',
  disabled: false,
  schemaKey: '',
})

const emit = defineEmits<{
  'update:modelValue': [val: string | number | null]
}>()

function handleInput(e: Event) {
  const target = e.target as HTMLInputElement
  const raw = target.value
  if (raw === '') {
    emit('update:modelValue', null)
  } else {
    emit('update:modelValue', raw)
  }
}
</script>

<template>
  <div class="space-y-1.5">
    <div class="flex items-center justify-between gap-2">
      <div class="flex items-center gap-1 min-w-0">
        <label class="block text-xs font-semibold text-slate-700 truncate">
          {{ label }}
        </label>
        <FieldHelpTip
          v-if="schemaKey"
          :schema-key="schemaKey"
        />
      </div>
      <span
        v-if="description"
        class="text-[11px] text-slate-400 shrink-0"
      >
        {{ description }}
      </span>
    </div>

    <input
      :type="type"
      :value="modelValue ?? ''"
      :placeholder="placeholder"
      :disabled="disabled"
      class="w-full px-3 py-1.5 bg-white border border-slate-200 rounded-lg text-xs font-mono outline-none focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all disabled:opacity-50"
      @input="handleInput"
    >
  </div>
</template>
