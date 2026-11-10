<script setup lang="ts">
import { ref, watch, computed } from 'vue'
import { onBeforeRouteLeave } from 'vue-router'
import YAML from 'yaml'
import type { ParsedConfigYaml, ModelProfileItem, ModelOverrideMap, CompatConfig } from './types.ts'
import ProfileCard from './ProfileCard.vue'
import ModelOverrideCard from './ModelOverrideCard.vue'
import CompatConfigSection from './CompatConfigSection.vue'
import SchemaDictionaryModal from './SchemaDictionaryModal.vue'
import ConfigSwitch from './ConfigSwitch.vue'
import FieldHelpTip from './FieldHelpTip.vue'
import Button from '@/components/ui/Button.vue'
import { systemApi } from '@/api/system.ts'
import { useToastStore } from '@/stores/toast.ts'
import { useConfirm } from '@/composables/useConfirm.ts'
import {
  FileCode,
  SlidersHorizontal,
  Save,
  Plus,
  RotateCcw,
  Layers,
  AlertCircle,
  BookOpen,
  Search,
  ChevronsUpDown,
} from 'lucide-vue-next'

const props = defineProps<{
  initialYaml?: string
}>()

const emit = defineEmits<{
  saved: []
}>()

const toast = useToastStore()
const { confirm } = useConfirm()

const activeTab = ref<'visual' | 'yaml'>('visual')
const rawYaml = ref(props.initialYaml || '')
const parsedConfig = ref<ParsedConfigYaml>({})
const yamlParseError = ref<string | null>(null)
const saving = ref(false)
const dictModalOpen = ref(false)
const filterKeyword = ref('')
const forceAllProfilesExpanded = ref<boolean | undefined>(undefined)

function toggleAllProfiles() {
  forceAllProfilesExpanded.value = !forceAllProfilesExpanded.value
}

const filteredProfilesWithIndex = computed(() => {
  const list = parsedConfig.value.model_defaults?.profiles || []
  const kw = filterKeyword.value.trim().toLowerCase()
  return list
    .map((profile, originalIndex) => ({ profile, originalIndex }))
    .filter(({ profile }) => {
      if (!kw) return true
      const inName = profile.name.toLowerCase().includes(kw)
      const inContains = profile.match?.contains?.some((c) => c.toLowerCase().includes(kw))
      const inPrefix = profile.match?.prefixes?.some((p) => p.toLowerCase().includes(kw))
      const inExact = profile.match?.exact?.some((e) => e.toLowerCase().includes(kw))
      return inName || inContains || inPrefix || inExact
    })
})

const filteredModels = computed(() => {
  const allModels = parsedConfig.value.model_defaults?.models || {}
  const kw = filterKeyword.value.trim().toLowerCase()
  if (!kw) return allModels
  const result: ModelOverrideMap = {}
  for (const [key, val] of Object.entries(allModels)) {
    if (key.toLowerCase().includes(kw)) {
      result[key] = val
    }
  }
  return result
})

function parseYamlToState(content: string): boolean {
  try {
    const doc = YAML.parse(content) || {}
    if (typeof doc !== 'object' || doc === null) {
      throw new Error('YAML 顶层必须是字典结构')
    }
    parsedConfig.value = doc as ParsedConfigYaml
    yamlParseError.value = null
    return true
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : 'YAML 解析失败'
    yamlParseError.value = msg
    return false
  }
}

function syncStateToYaml(): string {
  try {
    const dumped = YAML.stringify(parsedConfig.value, {
      indent: 2,
    })
    rawYaml.value = dumped
    return dumped
  } catch (err) {
    console.error('序列化 YAML 失败:', err)
    return rawYaml.value
  }
}

watch(
  () => props.initialYaml,
  (val) => {
    if (val !== undefined) {
      rawYaml.value = val
      parseYamlToState(val)
    }
  },
  { immediate: true }
)

function switchTab(tab: 'visual' | 'yaml') {
  if (tab === 'yaml') {
    syncStateToYaml()
  } else {
    const ok = parseYamlToState(rawYaml.value)
    if (!ok) {
      toast.error('当前 YAML 语法有误，无法切换至可视化视图: ' + yamlParseError.value)
      return
    }
  }
  activeTab.value = tab
}

function updateCompat(nextCompat: CompatConfig) {
  parsedConfig.value = {
    ...parsedConfig.value,
    compat: nextCompat,
  }
}

function handleAddProfile() {
  const currentProfiles = parsedConfig.value.model_defaults?.profiles || []
  const newProfile: ModelProfileItem = {
    name: `profile_${currentProfiles.length + 1}`,
    match: {
      prefixes: ['gemini-'],
    },
    safety_settings: {
      Harassment: 5,
      Hate: 5,
      'Sexually Explicit': 5,
      'Dangerous Content': 5,
    },
    drop_unsupported_params: true,
  }
  const nextModelDefaults = {
    ...(parsedConfig.value.model_defaults || {}),
    profiles: [...currentProfiles, newProfile],
  }
  parsedConfig.value = {
    ...parsedConfig.value,
    model_defaults: nextModelDefaults,
  }
}

function updateProfile(index: number, updated: ModelProfileItem) {
  const currentProfiles = [...(parsedConfig.value.model_defaults?.profiles || [])]
  currentProfiles[index] = updated
  const nextModelDefaults = {
    ...(parsedConfig.value.model_defaults || {}),
    profiles: currentProfiles,
  }
  parsedConfig.value = {
    ...parsedConfig.value,
    model_defaults: nextModelDefaults,
  }
}

function deleteProfile(index: number) {
  const currentProfiles = [...(parsedConfig.value.model_defaults?.profiles || [])]
  currentProfiles.splice(index, 1)
  const nextModelDefaults = {
    ...(parsedConfig.value.model_defaults || {}),
    profiles: currentProfiles,
  }
  parsedConfig.value = {
    ...parsedConfig.value,
    model_defaults: nextModelDefaults,
  }
}

function updateModelsOverride(models: ModelOverrideMap) {
  const nextModelDefaults = {
    ...(parsedConfig.value.model_defaults || {}),
    models,
  }
  parsedConfig.value = {
    ...parsedConfig.value,
    model_defaults: nextModelDefaults,
  }
}

function updateGlobalDropUnsupported(val: boolean) {
  const nextModelDefaults = {
    ...(parsedConfig.value.model_defaults || {}),
    drop_unsupported_params: val,
  }
  parsedConfig.value = {
    ...parsedConfig.value,
    model_defaults: nextModelDefaults,
  }
}

async function handleResetDefaults() {
  const ok = await confirm({
    title: '重置模型默认规则',
    message: '确定要将模型分组规则重置为官方推荐默认预设吗？（您的 API Keys、调度器与配额配置将被完整保留）',
    confirmText: '确认重置',
    variant: 'warning',
  })
  if (ok) {
    const nextDoc: ParsedConfigYaml = {
      ...parsedConfig.value,
      model_defaults: {
        drop_unsupported_params: false,
        profiles: [
          {
            name: 'image_models',
            match: {
              contains: ['image', 'imagen'],
            },
            is_image_model: true,
            disable_safety_settings: true,
          },
          {
            name: 'video_models',
            match: {
              prefixes: ['veo-'],
              contains: ['veo', 'omni'],
            },
            disable_safety_settings: true,
          },
          {
            name: 'gemma_models',
            match: {
              prefixes: ['gemma-'],
            },
            safety_settings: {
              Harassment: 5,
              Hate: 5,
              'Sexually Explicit': 5,
              'Dangerous Content': 5,
            },
          },
          {
            name: 'gemini_models',
            match: {
              prefixes: ['gemini-'],
            },
            safety_settings: {
              Harassment: 5,
              Hate: 5,
              'Sexually Explicit': 5,
              'Dangerous Content': 5,
            },
            drop_unsupported_params: true,
          },
        ],
        models: {},
      },
    }
    parsedConfig.value = nextDoc
    syncStateToYaml()
    toast.info('已载入推荐默认规则，请点击右上角「保存并热重载」使其生效')
  }
}

async function handleSave() {
  saving.value = true
  let contentToSave = rawYaml.value
  if (activeTab.value === 'visual') {
    contentToSave = syncStateToYaml()
  } else {
    const ok = parseYamlToState(rawYaml.value)
    if (!ok) {
      toast.error('YAML 格式错误: ' + yamlParseError.value)
      saving.value = false
      return
    }
  }

  try {
    await systemApi.updateConfigYaml(contentToSave)
    toast.success('配置已保存并完成热重载')
    emit('saved')
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '保存配置失败'
    toast.error(msg)
  } finally {
    saving.value = false
  }
}

function handleYamlKeydown(e: KeyboardEvent) {
  if (e.key === 'Tab') {
    e.preventDefault()
    const target = e.target as HTMLTextAreaElement
    const start = target.selectionStart
    const end = target.selectionEnd
    const val = rawYaml.value
    rawYaml.value = val.substring(0, start) + '  ' + val.substring(end)
    requestAnimationFrame(() => {
      target.selectionStart = target.selectionEnd = start + 2
    })
  }
}

const hasUnsavedChanges = computed(() => {
  const current = activeTab.value === 'visual' ? syncStateToYaml() : rawYaml.value
  return Boolean(props.initialYaml && current.trim() !== props.initialYaml.trim())
})

onBeforeRouteLeave(async (_to, _from, next) => {
  if (hasUnsavedChanges.value) {
    const ok = await confirm({
      title: '存在未保存的配置修改',
      message: '当前配置有未保存的修改，离开本页将丢失修改，确定离开吗？',
      confirmText: '放弃修改并离开',
      variant: 'warning',
    })
    next(ok)
  } else {
    next()
  }
})
</script>

<template>
  <div class="bg-white border border-slate-200/90 rounded-xl p-5 shadow-2xs space-y-5">
    <!-- Header & Mode Switcher -->
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-100 pb-4">
      <div class="flex items-center gap-2.5">
        <div class="w-8 h-8 rounded-lg bg-slate-900 text-brand-400 flex items-center justify-center shrink-0">
          <SlidersHorizontal class="w-4 h-4" />
        </div>
        <div>
          <h3 class="font-semibold text-slate-900 text-sm">
            模型规则、协议映射与配置文件 (config.yaml)
          </h3>
          <p class="text-[11px] text-slate-400 mt-0.5">
            支持可视化编辑兼容层映射、模型安全策略与生成参数，或直接编辑底层 YAML 并热重载
          </p>
        </div>
      </div>

      <!-- Controls & Actions -->
      <div class="flex items-center gap-2 flex-wrap">
        <Button
          variant="secondary"
          size="sm"
          title="查看所有配置项详细规范与推荐说明"
          @click="dictModalOpen = true"
        >
          <BookOpen class="w-3.5 h-3.5 text-brand-600" />
          <span>参数字典</span>
        </Button>

        <!-- Tab Pill Toggle -->
        <div class="flex items-center bg-slate-100 p-1 rounded-lg text-xs font-medium">
          <button
            type="button"
            class="px-2.5 py-1 rounded-md transition-all flex items-center gap-1.5 cursor-pointer"
            :class="activeTab === 'visual' ? 'bg-white text-slate-900 shadow-2xs font-semibold' : 'text-slate-500 hover:text-slate-900'"
            @click="switchTab('visual')"
          >
            <SlidersHorizontal class="w-3.5 h-3.5" />
            <span>可视化面板</span>
          </button>
          <button
            type="button"
            class="px-2.5 py-1 rounded-md transition-all flex items-center gap-1.5 cursor-pointer"
            :class="activeTab === 'yaml' ? 'bg-white text-slate-900 shadow-2xs font-semibold' : 'text-slate-500 hover:text-slate-900'"
            @click="switchTab('yaml')"
          >
            <FileCode class="w-3.5 h-3.5" />
            <span>YAML 源码</span>
          </button>
        </div>

        <Button
          variant="ghost"
          size="sm"
          title="重置模型分组为官方推荐规则"
          @click="handleResetDefaults"
        >
          <RotateCcw class="w-3.5 h-3.5" />
          <span class="hidden md:inline">重置默认</span>
        </Button>

        <Button
          variant="primary"
          size="sm"
          :loading="saving"
          @click="handleSave"
        >
          <Save class="w-3.5 h-3.5" />
          <span>保存并热重载</span>
        </Button>
      </div>
    </div>

    <!-- Error Banner if YAML is invalid -->
    <div
      v-if="yamlParseError"
      class="p-3.5 bg-rose-50 border border-rose-200 rounded-lg text-xs text-rose-800 flex items-start gap-2.5"
    >
      <AlertCircle class="w-4 h-4 text-rose-600 shrink-0 mt-0.5" />
      <div>
        <div class="font-semibold">
          YAML 语法解析错误
        </div>
        <div class="mt-0.5 font-mono text-[11px]">
          {{ yamlParseError }}
        </div>
      </div>
    </div>

    <!-- Mode 1: Visual Form Editor -->
    <div
      v-if="activeTab === 'visual'"
      class="space-y-6"
    >
      <!-- OpenAI / Claude Protocol Compatibility Mapping Section -->
      <CompatConfigSection
        :compat="parsedConfig.compat"
        @update:compat="updateCompat"
      />

      <!-- Global Drop Unsupported Params Switch -->
      <div class="p-4 bg-slate-50/80 border border-slate-200/80 rounded-xl flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <div>
          <div class="text-xs font-bold text-slate-900 flex items-center gap-1.5">
            <span>全局自动过滤不支持参数 (drop_unsupported_params)</span>
            <span class="text-[10px] text-brand-700 bg-brand-50 px-1.5 py-0.5 rounded font-mono border border-brand-200/60 font-semibold">全局默认</span>
            <FieldHelpTip schema-key="system.drop_unsupported_params" />
          </div>
          <p class="text-[11px] text-slate-400 mt-0.5">
            自动过滤下游客户端传入的未知安全类别或模型不支持的工具，避免上游 400 报错阻断
          </p>
        </div>
        <div class="shrink-0">
          <ConfigSwitch
            :model-value="Boolean(parsedConfig.model_defaults?.drop_unsupported_params)"
            label="启用全局自动过滤"
            @update:model-value="updateGlobalDropUnsupported($event)"
          />
        </div>
      </div>

      <!-- Search Filter Bar for Profiles & Overrides -->
      <div class="relative">
        <Search class="w-3.5 h-3.5 text-slate-400 absolute left-3.5 top-1/2 -translate-y-1/2 pointer-events-none" />
        <input
          v-model="filterKeyword"
          type="text"
          placeholder="快速筛选模型规则组或单模型覆盖名称（如：image、gemini、gemma）…"
          class="w-full pl-9 pr-4 py-2 bg-slate-50 border border-slate-200 rounded-lg text-xs outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 font-mono transition-all"
        >
      </div>

      <!-- Section 1: Model Profiles -->
      <div class="space-y-3.5">
        <div class="flex items-center justify-between border-b border-slate-100 pb-2.5">
          <div>
            <h4 class="text-xs font-bold text-slate-900 flex items-center gap-2">
              <Layers class="w-4 h-4 text-brand-600" />
              <span>模型分组规则 (Model Profiles)</span>
              <span class="px-1.5 py-0.5 text-[10px] font-mono bg-slate-100 text-slate-600 rounded tabular-nums">
                {{ filteredProfilesWithIndex.length }} 组
              </span>
            </h4>
          </div>

          <div class="flex items-center gap-2">
            <Button
              variant="ghost"
              size="xs"
              @click="toggleAllProfiles"
            >
              <ChevronsUpDown class="w-3.5 h-3.5 text-slate-400" />
              <span>{{ forceAllProfilesExpanded ? '折叠全部' : '展开全部' }}</span>
            </Button>
            <Button
              variant="secondary"
              size="xs"
              @click="handleAddProfile"
            >
              <Plus class="w-3.5 h-3.5" />
              <span>添加规则组</span>
            </Button>
          </div>
        </div>

        <div
          v-if="filteredProfilesWithIndex.length"
          class="space-y-3"
        >
          <ProfileCard
            v-for="item in filteredProfilesWithIndex"
            :key="item.originalIndex"
            :profile="item.profile"
            :index="item.originalIndex"
            :force-expanded="forceAllProfilesExpanded"
            @update:profile="updateProfile(item.originalIndex, $event)"
            @delete="deleteProfile(item.originalIndex)"
          />
        </div>
        <div
          v-else-if="filterKeyword.trim()"
          class="p-6 text-center text-slate-400 text-xs border border-dashed border-slate-200 rounded-xl"
        >
          未找到匹配 "{{ filterKeyword }}" 的模型分组规则
        </div>
        <div
          v-else
          class="p-8 text-center text-slate-400 text-xs border border-dashed border-slate-200 rounded-xl"
        >
          未配置任何模型分组规则。点击上方按钮添加规则。
        </div>
      </div>

      <!-- Section 2: Specific Model Overrides -->
      <ModelOverrideCard
        :models="filteredModels"
        @update:models="updateModelsOverride"
      />
    </div>

    <!-- Mode 2: Raw YAML Code Editor -->
    <div
      v-else
      class="space-y-2"
    >
      <div class="flex items-center justify-between text-xs text-slate-400 font-mono">
        <span>config.yaml 完整源码</span>
        <span>保存后自动热重载，无需重启进程</span>
      </div>
      <textarea
        v-model="rawYaml"
        rows="24"
        spellcheck="false"
        placeholder="正在加载 config.yaml…"
        class="w-full p-4 font-mono text-xs bg-slate-950 text-slate-100 rounded-xl outline-none focus:ring-2 focus:ring-brand-500 leading-relaxed border border-slate-800"
        @keydown="handleYamlKeydown"
      />
    </div>

    <!-- Schema Specification Dictionary Modal -->
    <SchemaDictionaryModal v-model="dictModalOpen" />
  </div>
</template>
