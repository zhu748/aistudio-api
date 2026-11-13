<script setup lang="ts">
import { ref } from 'vue'
import type { ModelOverrideMap, ModelOverrideItem, ThinkingConfig } from './types.ts'
import {
  TOOL_SUGGESTIONS,
  IMAGE_MODE_OPTIONS,
  THINKING_LEVEL_OPTIONS,
  MEDIA_RESOLUTION_OPTIONS,
} from './schema.ts'
import { useConfirm } from '@/composables/useConfirm.ts'
import ConfigSelect from './ConfigSelect.vue'
import ConfigTagList from './ConfigTagList.vue'
import ConfigSafetyGrid from './ConfigSafetyGrid.vue'
import ConfigSwitch from './ConfigSwitch.vue'
import FieldHelpTip from './FieldHelpTip.vue'
import Button from '@/components/ui/Button.vue'
import { Plus, Trash2, ChevronDown, ChevronRight, Layers, Cpu, Wrench } from 'lucide-vue-next'

const props = defineProps<{
  models: ModelOverrideMap
}>()

const emit = defineEmits<{
  'update:models': [val: ModelOverrideMap]
}>()

const { confirm } = useConfirm()
const newModelName = ref('')
const expandedMap = ref<Record<string, boolean>>({})

function toggleExpand(modelKey: string) {
  expandedMap.value[modelKey] = !expandedMap.value[modelKey]
}

function handleAddModel() {
  const name = newModelName.value.trim().toLowerCase().replace(/^models\//, '')
  if (!name) return

  const next: ModelOverrideMap = { ...(props.models || {}) }
  if (!next[name]) {
    next[name] = {}
    expandedMap.value[name] = true
    emit('update:models', next)
  }
  newModelName.value = ''
}

async function handleDeleteModel(modelKey: string) {
  const ok = await confirm({
    title: '删除单模型覆盖规则',
    message: `确定要删除模型「${modelKey}」的专用覆盖规则吗？`,
    confirmText: '确认删除',
    variant: 'danger',
  })
  if (ok) {
    const next: ModelOverrideMap = { ...(props.models || {}) }
    delete next[modelKey]
    emit('update:models', next)
  }
}

function updateModelField<K extends keyof ModelOverrideItem>(
  modelKey: string,
  field: K,
  val: ModelOverrideItem[K],
) {
  const next: ModelOverrideMap = { ...(props.models || {}) }
  const current = next[modelKey] || {}
  next[modelKey] = { ...current, [field]: val }
  emit('update:models', next)
}

function updateModelGenField(modelKey: string, key: string, val: unknown) {
  const next: ModelOverrideMap = { ...(props.models || {}) }
  const current = next[modelKey] || {}
  const currentGen = { ...(current.generation_config_defaults || {}) }
  if (val === null || val === undefined) {
    delete (currentGen as Record<string, unknown>)[key]
  } else {
    ;(currentGen as Record<string, unknown>)[key] = val
  }
  next[modelKey] = {
    ...current,
    generation_config_defaults: Object.keys(currentGen).length ? currentGen : undefined,
  }
  emit('update:models', next)
}

function updateModelThinkingConfig(modelKey: string, level: string | null) {
  const next: ModelOverrideMap = { ...(props.models || {}) }
  const current = next[modelKey] || {}
  const currentGen = { ...(current.generation_config_defaults || {}) }
  if (level === null) {
    delete currentGen.thinking_config
  } else {
    const prevThinking = currentGen.thinking_config || { mode: 1 }
    currentGen.thinking_config = { ...prevThinking, level: level as ThinkingConfig['level'] }
  }
  next[modelKey] = {
    ...current,
    generation_config_defaults: Object.keys(currentGen).length ? currentGen : undefined,
  }
  emit('update:models', next)
}
</script>

<template>
  <div class="space-y-3.5">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-100 pb-2.5">
      <div>
        <h4 class="text-xs font-bold text-slate-900 flex items-center gap-2">
          <Layers class="w-4 h-4 text-brand-600" />
          <span>单模型覆盖规则 (Model Overrides)</span>
        </h4>
      </div>

      <!-- Add Input -->
      <div class="flex items-center gap-2">
        <input
          v-model="newModelName"
          type="text"
          placeholder="例如: gemini-3.7-flash"
          class="px-3 py-1.5 bg-slate-50 border border-slate-200 rounded-lg text-xs font-mono outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20"
          @keydown.enter.prevent="handleAddModel"
        >
        <Button
          variant="primary"
          size="xs"
          :disabled="!newModelName.trim()"
          @click="handleAddModel"
        >
          <Plus class="w-3.5 h-3.5" />
          <span>添加覆盖</span>
        </Button>
      </div>
    </div>

    <!-- Override List -->
    <div
      v-if="Object.keys(models || {}).length"
      class="space-y-3"
    >
      <div
        v-for="(override, modelKey) in models"
        :key="modelKey"
        class="bg-white border border-slate-200/90 rounded-xl shadow-2xs"
      >
        <!-- Header -->
        <div
          class="px-4 py-2.5 bg-slate-50/70 flex items-center justify-between gap-3 cursor-pointer select-none hover:bg-slate-100/60 transition-colors"
          :class="expandedMap[String(modelKey)] ? 'border-b border-slate-100 rounded-t-xl' : 'rounded-xl'"
          @click="toggleExpand(String(modelKey))"
        >
          <div class="flex items-center gap-2 font-mono text-xs font-bold text-slate-900">
            <button
              type="button"
              class="text-slate-400 p-0.5"
            >
              <ChevronDown
                v-if="expandedMap[String(modelKey)]"
                class="w-4 h-4"
              />
              <ChevronRight
                v-else
                class="w-4 h-4"
              />
            </button>
            <span class="text-brand-700 bg-brand-50 px-2 py-0.5 rounded border border-brand-200/60">
              {{ modelKey }}
            </span>
            <span
              v-if="override.is_image_model"
              class="px-1.5 py-0.5 text-[10px] font-semibold rounded bg-sky-50 text-sky-700 border border-sky-200/70"
            >
              生图模型
            </span>
          </div>

          <button
            type="button"
            class="p-1.5 text-slate-400 hover:text-rose-600 hover:bg-rose-50 rounded-lg transition-colors cursor-pointer"
            title="删除此模型覆盖"
            @click.stop="handleDeleteModel(String(modelKey))"
          >
            <Trash2 class="w-3.5 h-3.5" />
          </button>
        </div>

        <!-- Body -->
        <div
          v-if="expandedMap[String(modelKey)]"
          class="p-4 space-y-4"
        >
          <!-- Image Model Switch -->
          <ConfigSwitch
            :model-value="Boolean(override.is_image_model)"
            label="作为生图模型 (is_image_model)"
            description="将该模型设为生图模式，启用多模态图片编码与专用处理管线"
            schema-key="profile.is_image_model"
            @update:model-value="updateModelField(String(modelKey), 'is_image_model', $event)"
          />

          <!-- Generation Config Defaults -->
          <div class="p-3.5 bg-slate-50/70 border border-slate-200/70 rounded-xl space-y-3">
            <div class="flex items-center gap-1.5 text-xs font-bold text-slate-800">
              <Cpu class="w-4 h-4 text-brand-600" />
              <span>生成参数覆盖</span>
              <FieldHelpTip schema-key="generation.defaults" />
            </div>

            <div class="grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 gap-3">
              <ConfigSelect
                :model-value="override.generation_config_defaults?.image_output_mode"
                label="图片输出模式"
                :options="IMAGE_MODE_OPTIONS"
                schema-key="generation.image_output_mode"
                @update:model-value="updateModelGenField(String(modelKey), 'image_output_mode', $event)"
              />

              <ConfigSelect
                :model-value="override.generation_config_defaults?.thinking_config?.level"
                label="思考强度等级"
                :options="THINKING_LEVEL_OPTIONS"
                schema-key="generation.thinking_level"
                @update:model-value="updateModelThinkingConfig(String(modelKey), $event as string | null)"
              />

              <ConfigSelect
                :model-value="override.generation_config_defaults?.media_resolution"
                label="多模态输入分辨率"
                :options="MEDIA_RESOLUTION_OPTIONS"
                schema-key="generation.media_resolution"
                @update:model-value="updateModelGenField(String(modelKey), 'media_resolution', $event)"
              />
            </div>

            <div class="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-1">
              <ConfigSwitch
                :model-value="Boolean(override.drop_unsupported_params)"
                label="自动过滤不支持的参数"
                description="自动过滤未知安全类别与不兼容工具"
                schema-key="profile.drop_unsupported_params"
                @update:model-value="updateModelField(String(modelKey), 'drop_unsupported_params', $event)"
              />

              <ConfigSwitch
                :model-value="Boolean(override.disable_safety_settings)"
                label="完全关闭安全规则"
                description="生图模型通常开启此项以避免被安全机制误拦截"
                schema-key="safety.disable_safety_settings"
                @update:model-value="updateModelField(String(modelKey), 'disable_safety_settings', $event)"
              />
            </div>

            <!-- Clear Indexes -->
            <div class="pt-1">
              <ConfigTagList
                :model-value="override.clear_generation_config_indexes || []"
                label="清空特定字段下标"
                description="针对该模型发送前清除的 wire 数组索引 (如 7, 13, 17)"
                :is-number="true"
                placeholder="输入数字下标按回车"
                schema-key="generation.clear_indexes"
                @update:model-value="updateModelField(String(modelKey), 'clear_generation_config_indexes', $event as number[])"
              />
            </div>
          </div>

          <!-- Tools -->
          <div class="p-3.5 bg-slate-50/70 border border-slate-200/70 rounded-xl space-y-2">
            <div class="flex items-center gap-1.5 text-xs font-bold text-slate-800">
              <Wrench class="w-4 h-4 text-brand-600" />
              <span>模型专用工具</span>
              <FieldHelpTip schema-key="profile.default_tools" />
            </div>
            <ConfigTagList
              :model-value="override.default_tools || []"
              label="覆盖通用 profile 的工具列表"
              description="客户端未显式传 tools 时挂载的专用工具"
              :suggestions="TOOL_SUGGESTIONS"
              @update:model-value="updateModelField(String(modelKey), 'default_tools', $event as string[])"
            />
          </div>

          <!-- Safety Settings -->
          <div v-if="!override.disable_safety_settings">
            <ConfigSafetyGrid
              :model-value="override.safety_settings || {}"
              @update:model-value="updateModelField(String(modelKey), 'safety_settings', $event)"
            />
          </div>
        </div>
      </div>
    </div>

    <div
      v-else
      class="p-6 text-center text-slate-400 text-xs border border-dashed border-slate-200 rounded-xl bg-slate-50/40"
    >
      暂无单模型覆盖配置，全部模型遵循上方 Profiles 分组规则。
    </div>
  </div>
</template>
