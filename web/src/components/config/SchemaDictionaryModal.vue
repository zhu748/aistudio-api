<script setup lang="ts">
import { ref, computed } from 'vue'
import Modal from '@/components/ui/Modal.vue'
import Button from '@/components/ui/Button.vue'
import { CONFIG_SCHEMA, SCHEMA_CATEGORIES, type FieldDefinition } from './schema.ts'
import { Search, BookOpen, CheckCircle2, Tag } from 'lucide-vue-next'

defineProps<{
  modelValue: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [val: boolean]
}>()

const searchQuery = ref('')
const selectedCategory = ref<string>('all')

const allFields = computed<FieldDefinition[]>(() => Object.values(CONFIG_SCHEMA))

const filteredFields = computed(() => {
  const query = searchQuery.value.trim().toLowerCase()
  return allFields.value.filter((f) => {
    if (selectedCategory.value !== 'all' && f.category !== selectedCategory.value) {
      return false
    }
    if (!query) return true
    const inTitle = f.title.toLowerCase().includes(query)
    const inKey = f.key.toLowerCase().includes(query)
    const inDesc = f.description.toLowerCase().includes(query)
    const inExp = f.explanation.toLowerCase().includes(query)
    const inTags = f.tags?.some((t) => t.toLowerCase().includes(query))
    return inTitle || inKey || inDesc || inExp || inTags
  })
})
</script>

<template>
  <Modal
    :model-value="modelValue"
    max-width="2xl"
    title="配置项规范字典"
    subtitle="详细说明 config.yaml 各配置字段的业务作用与推荐值"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="space-y-4">
      <!-- Search & Category Filters -->
      <div class="space-y-2.5">
        <div class="relative">
          <Search class="w-4 h-4 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
          <input
            v-model="searchQuery"
            type="text"
            placeholder="搜索配置项名称、键名、关键词（如：生图、思考、安全、工具）…"
            class="w-full pl-9 pr-4 py-2 bg-slate-50 border border-slate-200 rounded-lg text-xs outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all font-mono"
          >
        </div>

        <!-- Category Pills -->
        <div class="flex items-center gap-1.5 flex-wrap text-xs">
          <button
            type="button"
            class="px-2.5 py-1 rounded-md transition-all cursor-pointer"
            :class="selectedCategory === 'all' ? 'bg-slate-900 text-white font-semibold' : 'text-slate-600 hover:bg-slate-100'"
            @click="selectedCategory = 'all'"
          >
            全部 ({{ allFields.length }})
          </button>
          <button
            v-for="cat in SCHEMA_CATEGORIES"
            :key="cat.key"
            type="button"
            class="px-2.5 py-1 rounded-md transition-all cursor-pointer"
            :class="selectedCategory === cat.key ? 'bg-brand-600 text-white font-semibold' : 'text-slate-600 hover:bg-slate-100'"
            @click="selectedCategory = cat.key"
          >
            {{ cat.label }}
          </button>
        </div>
      </div>

      <!-- Field List -->
      <div class="space-y-2.5 max-h-[58vh] overflow-y-auto pr-1">
        <div
          v-for="field in filteredFields"
          :key="field.key"
          class="p-3.5 bg-white border border-slate-200/90 rounded-xl space-y-2 hover:border-slate-300 transition-colors"
        >
          <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-1 border-b border-slate-100 pb-2">
            <div class="flex items-center gap-2">
              <BookOpen class="w-3.5 h-3.5 text-brand-600" />
              <span class="font-bold text-slate-900 text-xs">{{ field.title }}</span>
              <span class="font-mono text-[10px] text-slate-500 bg-slate-50 px-1.5 py-0.5 rounded border border-slate-200/70">
                {{ field.key }}
              </span>
            </div>

            <div class="flex items-center gap-1.5">
              <span
                v-for="t in field.tags || []"
                :key="t"
                class="inline-flex items-center gap-0.5 text-[10px] px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 font-medium"
              >
                <Tag class="w-2.5 h-2.5" />
                <span>{{ t }}</span>
              </span>
            </div>
          </div>

          <div class="text-xs text-slate-700 leading-relaxed">
            {{ field.description }}
          </div>

          <div class="p-2.5 bg-slate-50 rounded-lg text-[11px] text-slate-500 leading-relaxed">
            <strong class="text-slate-700">设计背景与原理:</strong> {{ field.explanation }}
          </div>

          <div
            v-if="field.recommendedValue !== undefined"
            class="flex items-center gap-1.5 text-[11px] font-semibold text-emerald-700 bg-emerald-50 px-2.5 py-1 rounded-md border border-emerald-200/60"
          >
            <CheckCircle2 class="w-3.5 h-3.5 text-emerald-600" />
            <span>推荐值: {{ JSON.stringify(field.recommendedValue) }}</span>
          </div>
        </div>

        <div
          v-if="!filteredFields.length"
          class="py-12 text-center text-slate-400 text-xs"
        >
          未找到匹配的配置项规范
        </div>
      </div>
    </div>

    <template #footer>
      <Button
        variant="secondary"
        size="sm"
        @click="emit('update:modelValue', false)"
      >
        关闭
      </Button>
    </template>
  </Modal>
</template>
