<script setup lang="ts">
import FieldHelpTip from './FieldHelpTip.vue'

interface Props {
  modelValue: boolean
  label: string
  description?: string
  disabled?: boolean
  schemaKey?: string
}

const props = withDefaults(defineProps<Props>(), {
  description: '',
  disabled: false,
  schemaKey: '',
})

const emit = defineEmits<{
  'update:modelValue': [val: boolean]
}>()

function toggle() {
  if (!props.disabled) {
    emit('update:modelValue', !props.modelValue)
  }
}
</script>

<template>
  <div
    class="flex items-center justify-between p-3 bg-white border border-slate-200/80 rounded-lg transition-colors hover:border-slate-300 select-none cursor-pointer"
    :class="{ 'opacity-60 cursor-not-allowed': disabled }"
    @click="toggle"
  >
    <div class="pr-4 min-w-0">
      <div class="text-xs font-semibold text-slate-900 flex items-center gap-1">
        <span>{{ label }}</span>
        <span
          v-if="schemaKey"
          class="inline-flex items-center"
          @click.stop
        >
          <FieldHelpTip :schema-key="schemaKey" />
        </span>
      </div>
      <div
        v-if="description"
        class="text-[11px] text-slate-400 mt-0.5"
      >
        {{ description }}
      </div>
    </div>

    <button
      type="button"
      :disabled="disabled"
      class="relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none"
      :class="modelValue ? 'bg-brand-600' : 'bg-slate-300'"
      @click.stop="toggle"
    >
      <span
        class="pointer-events-none inline-block h-4 w-4 transform rounded-full bg-white shadow-sm ring-0 transition duration-200 ease-in-out"
        :class="modelValue ? 'translate-x-4' : 'translate-x-0'"
      />
    </button>
  </div>
</template>
