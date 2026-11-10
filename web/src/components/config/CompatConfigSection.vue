<script setup lang="ts">
import { ref, computed } from 'vue'
import type { CompatConfig, CompatProtocolConfig } from './types.ts'
import Button from '@/components/ui/Button.vue'
import { ArrowRight, Plus, Trash2, GitCompareArrows } from 'lucide-vue-next'

const props = defineProps<{
  compat?: CompatConfig
}>()

const emit = defineEmits<{
  'update:compat': [val: CompatConfig]
}>()

const openaiFrom = ref('')
const openaiTo = ref('')
const claudeFrom = ref('')
const claudeTo = ref('')

const openaiConfig = computed<CompatProtocolConfig>(() => ({
  default_model: props.compat?.openai?.default_model || 'gemini-3.7-flash',
  model_mapping: props.compat?.openai?.model_mapping || {},
}))

const claudeConfig = computed<CompatProtocolConfig>(() => ({
  default_model: props.compat?.claude?.default_model || 'gemini-3.7-flash',
  model_mapping: props.compat?.claude?.model_mapping || {},
}))

function updateProtocol(protocol: 'openai' | 'claude', nextProto: CompatProtocolConfig) {
  const next: CompatConfig = {
    openai: { ...openaiConfig.value },
    claude: { ...claudeConfig.value },
    [protocol]: nextProto,
  }
  emit('update:compat', next)
}

function updateDefaultModel(protocol: 'openai' | 'claude', val: string) {
  const current = protocol === 'openai' ? openaiConfig.value : claudeConfig.value
  updateProtocol(protocol, {
    ...current,
    default_model: val.trim() || 'gemini-3.7-flash',
  })
}

function addMapping(protocol: 'openai' | 'claude') {
  const src = (protocol === 'openai' ? openaiFrom.value : claudeFrom.value).trim()
  const dst = (protocol === 'openai' ? openaiTo.value : claudeTo.value).trim()
  if (!src || !dst) return

  const current = protocol === 'openai' ? openaiConfig.value : claudeConfig.value
  const nextMap = { ...(current.model_mapping || {}), [src]: dst }
  updateProtocol(protocol, {
    ...current,
    model_mapping: nextMap,
  })

  if (protocol === 'openai') {
    openaiFrom.value = ''
    openaiTo.value = ''
  } else {
    claudeFrom.value = ''
    claudeTo.value = ''
  }
}

function removeMapping(protocol: 'openai' | 'claude', key: string) {
  const current = protocol === 'openai' ? openaiConfig.value : claudeConfig.value
  const nextMap = { ...(current.model_mapping || {}) }
  delete nextMap[key]
  updateProtocol(protocol, {
    ...current,
    model_mapping: nextMap,
  })
}
</script>

<template>
  <div class="space-y-3">
    <div class="flex items-center justify-between border-b border-slate-100 pb-2.5">
      <div>
        <h4 class="text-xs font-bold text-slate-900 flex items-center gap-2">
          <GitCompareArrows class="w-4 h-4 text-brand-600" />
          <span>OpenAI / Claude 兼容协议模型映射 (compat)</span>
        </h4>
        <p class="text-[11px] text-slate-400 mt-0.5">
          客户端请求 <code class="font-mono">gpt-* / o1* / claude-*</code> 等外部模型名时，按此规则映射或回退至指定 Gemini 模型；直接传 Gemini 模型名则原样透传
        </p>
      </div>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <!-- OpenAI Protocol Panel -->
      <div class="p-4 bg-slate-50/70 border border-slate-200/80 rounded-xl space-y-3.5">
        <div class="flex items-center justify-between">
          <span class="text-xs font-bold text-slate-800">OpenAI 协议 (/v1/chat/completions)</span>
          <span class="px-1.5 py-0.5 text-[10px] font-mono bg-white border border-slate-200 rounded text-slate-500">
            compat.openai
          </span>
        </div>

        <div class="space-y-1.5">
          <label class="block text-[11px] font-semibold text-slate-600">
            默认回退模型 (default_model)
          </label>
          <input
            :value="openaiConfig.default_model"
            type="text"
            placeholder="gemini-3.7-flash"
            class="w-full px-3 py-1.5 bg-white border border-slate-200 rounded-lg text-xs font-mono outline-none focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20"
            @change="updateDefaultModel('openai', ($event.target as HTMLInputElement).value)"
          >
        </div>

        <div class="space-y-2">
          <label class="block text-[11px] font-semibold text-slate-600">
            自定义模型映射 (支持精确匹配与前缀通配 *)
          </label>

          <!-- Existing Mappings -->
          <div
            v-if="Object.keys(openaiConfig.model_mapping || {}).length"
            class="space-y-1.5"
          >
            <div
              v-for="(targetModel, srcPattern) in openaiConfig.model_mapping"
              :key="String(srcPattern)"
              class="flex items-center justify-between gap-2 px-2.5 py-1.5 bg-white border border-slate-200/80 rounded-lg text-xs font-mono"
            >
              <div class="flex items-center gap-1.5 min-w-0 truncate">
                <span class="text-slate-700 font-semibold truncate">{{ srcPattern }}</span>
                <ArrowRight class="w-3 h-3 text-slate-400 shrink-0" />
                <span class="text-brand-700 font-semibold truncate">{{ targetModel }}</span>
              </div>
              <button
                type="button"
                class="text-slate-400 hover:text-rose-600 p-0.5 rounded cursor-pointer shrink-0"
                title="删除映射"
                @click="removeMapping('openai', String(srcPattern))"
              >
                <Trash2 class="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
          <div
            v-else
            class="text-[11px] text-slate-400 py-1.5 px-2.5 bg-white/60 rounded-lg border border-dashed border-slate-200"
          >
            未配置特殊映射，所有 gpt-* / o1* 请求默认路由至 {{ openaiConfig.default_model }}
          </div>

          <!-- Add New Mapping Row -->
          <div class="flex items-center gap-1.5 pt-1">
            <input
              v-model="openaiFrom"
              type="text"
              placeholder="如 gpt-4o*"
              class="flex-1 min-w-0 px-2.5 py-1.5 bg-white border border-slate-200 rounded-lg text-xs font-mono outline-none focus:border-brand-500"
              @keydown.enter.prevent="addMapping('openai')"
            >
            <ArrowRight class="w-3.5 h-3.5 text-slate-400 shrink-0" />
            <input
              v-model="openaiTo"
              type="text"
              placeholder="如 gemini-3.7-flash"
              class="flex-1 min-w-0 px-2.5 py-1.5 bg-white border border-slate-200 rounded-lg text-xs font-mono outline-none focus:border-brand-500"
              @keydown.enter.prevent="addMapping('openai')"
            >
            <Button
              variant="secondary"
              size="xs"
              :disabled="!openaiFrom.trim() || !openaiTo.trim()"
              @click="addMapping('openai')"
            >
              <Plus class="w-3 h-3" />
              <span>添加</span>
            </Button>
          </div>
        </div>
      </div>

      <!-- Claude Protocol Panel -->
      <div class="p-4 bg-slate-50/70 border border-slate-200/80 rounded-xl space-y-3.5">
        <div class="flex items-center justify-between">
          <span class="text-xs font-bold text-slate-800">Claude 协议 (/v1/messages)</span>
          <span class="px-1.5 py-0.5 text-[10px] font-mono bg-white border border-slate-200 rounded text-slate-500">
            compat.claude
          </span>
        </div>

        <div class="space-y-1.5">
          <label class="block text-[11px] font-semibold text-slate-600">
            默认回退模型 (default_model)
          </label>
          <input
            :value="claudeConfig.default_model"
            type="text"
            placeholder="gemini-3.7-flash"
            class="w-full px-3 py-1.5 bg-white border border-slate-200 rounded-lg text-xs font-mono outline-none focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20"
            @change="updateDefaultModel('claude', ($event.target as HTMLInputElement).value)"
          >
        </div>

        <div class="space-y-2">
          <label class="block text-[11px] font-semibold text-slate-600">
            自定义模型映射 (支持精确匹配与前缀通配 *)
          </label>

          <!-- Existing Mappings -->
          <div
            v-if="Object.keys(claudeConfig.model_mapping || {}).length"
            class="space-y-1.5"
          >
            <div
              v-for="(targetModel, srcPattern) in claudeConfig.model_mapping"
              :key="String(srcPattern)"
              class="flex items-center justify-between gap-2 px-2.5 py-1.5 bg-white border border-slate-200/80 rounded-lg text-xs font-mono"
            >
              <div class="flex items-center gap-1.5 min-w-0 truncate">
                <span class="text-slate-700 font-semibold truncate">{{ srcPattern }}</span>
                <ArrowRight class="w-3 h-3 text-slate-400 shrink-0" />
                <span class="text-brand-700 font-semibold truncate">{{ targetModel }}</span>
              </div>
              <button
                type="button"
                class="text-slate-400 hover:text-rose-600 p-0.5 rounded cursor-pointer shrink-0"
                title="删除映射"
                @click="removeMapping('claude', String(srcPattern))"
              >
                <Trash2 class="w-3.5 h-3.5" />
              </button>
            </div>
          </div>
          <div
            v-else
            class="text-[11px] text-slate-400 py-1.5 px-2.5 bg-white/60 rounded-lg border border-dashed border-slate-200"
          >
            未配置特殊映射，所有 claude-* 请求默认路由至 {{ claudeConfig.default_model }}
          </div>

          <!-- Add New Mapping Row -->
          <div class="flex items-center gap-1.5 pt-1">
            <input
              v-model="claudeFrom"
              type="text"
              placeholder="如 claude-opus-*"
              class="flex-1 min-w-0 px-2.5 py-1.5 bg-white border border-slate-200 rounded-lg text-xs font-mono outline-none focus:border-brand-500"
              @keydown.enter.prevent="addMapping('claude')"
            >
            <ArrowRight class="w-3.5 h-3.5 text-slate-400 shrink-0" />
            <input
              v-model="claudeTo"
              type="text"
              placeholder="如 gemini-4-argon"
              class="flex-1 min-w-0 px-2.5 py-1.5 bg-white border border-slate-200 rounded-lg text-xs font-mono outline-none focus:border-brand-500"
              @keydown.enter.prevent="addMapping('claude')"
            >
            <Button
              variant="secondary"
              size="xs"
              :disabled="!claudeFrom.trim() || !claudeTo.trim()"
              @click="addMapping('claude')"
            >
              <Plus class="w-3 h-3" />
              <span>添加</span>
            </Button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
