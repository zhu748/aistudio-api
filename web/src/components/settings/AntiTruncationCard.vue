<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { systemApi } from '@/api/system.ts'
import { useToastStore } from '@/stores/toast.ts'
import Button from '@/components/ui/Button.vue'
import ConfigSwitch from '@/components/config/ConfigSwitch.vue'
import {
  ShieldCheck,
  Save,
  Search,
  Info,
  Check,
  Boxes,
} from 'lucide-vue-next'

const toast = useToastStore()

interface AvailableModel {
  id: string
  displayName: string
}

const loading = ref(false)
const saving = ref(false)

const globalEnabled = ref(false)
const variantsEnabled = ref(false)
const selectedModels = ref<string[]>([])
const availableModels = ref<AvailableModel[]>([])
const searchKeyword = ref('')

const filteredModels = computed(() => {
  const kw = searchKeyword.value.trim().toLowerCase()
  if (!kw) return availableModels.value
  return availableModels.value.filter(
    (m) =>
      m.id.toLowerCase().includes(kw) ||
      m.displayName.toLowerCase().includes(kw),
  )
})

const selectedCount = computed(() => selectedModels.value.length)

function isSelected(id: string): boolean {
  return selectedModels.value.includes(id)
}

function toggleModel(id: string) {
  if (isSelected(id)) {
    selectedModels.value = selectedModels.value.filter((m) => m !== id)
  } else {
    selectedModels.value = [...selectedModels.value, id]
  }
}

async function loadConfig() {
  loading.value = true
  try {
    const res = await systemApi.getAntiTruncationConfig()
    globalEnabled.value = Boolean(res.enabled)
    variantsEnabled.value = Boolean(res.variants?.enabled)
    selectedModels.value = [...(res.variants?.models || [])]
    availableModels.value = res.available_models || []
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '获取抗截断配置失败'
    toast.error(msg)
  } finally {
    loading.value = false
  }
}

async function handleSave() {
  saving.value = true
  try {
    const res = await systemApi.updateAntiTruncationConfig({
      enabled: globalEnabled.value,
      variants_enabled: variantsEnabled.value,
      models: selectedModels.value,
    })
    globalEnabled.value = Boolean(res.enabled)
    variantsEnabled.value = Boolean(res.variants?.enabled)
    selectedModels.value = [...(res.variants?.models || [])]
    toast.success('抗截断传输配置已保存')
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '保存抗截断配置失败'
    toast.error(msg)
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  void loadConfig()
})
</script>

<template>
  <div class="bg-white border border-slate-200/90 rounded-xl p-5 shadow-2xs space-y-5">
    <!-- Card Header -->
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-100 pb-4">
      <div class="flex items-center gap-2.5">
        <div class="w-8 h-8 rounded-lg bg-slate-900 text-brand-400 flex items-center justify-center shrink-0">
          <ShieldCheck class="w-4 h-4" />
        </div>
        <div>
          <h3 class="font-semibold text-slate-900 text-sm">
            抗截断传输 (Anti-Truncation Transport)
          </h3>
          <p class="text-[11px] text-slate-400 mt-0.5">
            将模型正文封装至工具调用参数以绕开上游内容审查截断，网关侧自动无感还原
          </p>
        </div>
      </div>

      <div class="flex items-center gap-2">
        <Button
          variant="primary"
          size="sm"
          :loading="saving || loading"
          @click="handleSave"
        >
          <Save class="w-3.5 h-3.5" />
          <span>保存抗截断配置</span>
        </Button>
      </div>
    </div>

    <!-- Mode Switches -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <div class="p-3.5 bg-slate-50/70 border border-slate-200/80 rounded-xl">
        <ConfigSwitch
          :model-value="globalEnabled"
          label="全局开启（所有模型）"
          description="所有模型请求默认启用抗截断传输，无需选择后缀变体"
          @update:model-value="globalEnabled = $event"
        />
      </div>
      <div class="p-3.5 bg-slate-50/70 border border-slate-200/80 rounded-xl">
        <ConfigSwitch
          :model-value="variantsEnabled"
          label="模型变体模式（按模型挂载后缀）"
          description="为下方勾选的模型暴露 -at-stream（流式）与 -at（非流式）两个变体"
          @update:model-value="variantsEnabled = $event"
        />
      </div>
    </div>

    <!-- Variant Explanation -->
    <div
      v-if="variantsEnabled"
      class="p-3 bg-brand-50/60 border border-brand-200/70 rounded-lg flex items-start gap-2.5 text-xs text-slate-700 leading-relaxed"
    >
      <Info class="w-4 h-4 text-brand-600 shrink-0 mt-0.5" />
      <div>
        勾选模型后，客户端模型列表将额外暴露 <code class="font-mono px-1 py-0.5 rounded bg-white border border-brand-200/70 text-brand-700">&lt;model&gt;-at-stream</code>（流式逐段还原）与
        <code class="font-mono px-1 py-0.5 rounded bg-white border border-brand-200/70 text-brand-700">&lt;model&gt;-at</code>（完整缓冲下发）。也可通过请求头 <code class="font-mono">X-Anti-Truncation: 1/0</code> 按次覆盖。
      </div>
    </div>

    <!-- Model Multi-Select -->
    <div
      class="p-4 bg-slate-50/70 border border-slate-200/80 rounded-xl space-y-3 transition-opacity"
      :class="{ 'opacity-50 pointer-events-none': !variantsEnabled }"
    >
      <div class="flex items-center justify-between gap-3">
        <div class="flex items-center gap-1.5 text-xs font-semibold text-slate-700">
          <Boxes class="w-3.5 h-3.5 text-brand-600" />
          <span>选择暴露抗截断变体的模型</span>
        </div>
        <span
          v-if="selectedCount > 0"
          class="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[11px] font-mono tabular-nums font-semibold bg-brand-50 text-brand-700 border border-brand-200/70"
        >
          已选 {{ selectedCount }} 个
        </span>
      </div>

      <!-- Search -->
      <div class="relative">
        <Search class="w-3.5 h-3.5 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2" />
        <input
          v-model="searchKeyword"
          type="text"
          placeholder="搜索模型 ID 或名称…"
          class="w-full pl-8 pr-3 py-1.5 text-xs bg-white border border-slate-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-all"
        >
      </div>

      <!-- Selected Tags -->
      <div
        v-if="selectedCount > 0"
        class="flex flex-wrap gap-1.5"
      >
        <span
          v-for="id in selectedModels"
          :key="id"
          class="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[11px] font-mono font-medium bg-white text-slate-800 border border-slate-200 shadow-2xs"
        >
          <span>{{ id }}</span>
          <button
            type="button"
            class="text-slate-400 hover:text-rose-600 cursor-pointer ml-0.5"
            @click="toggleModel(id)"
          >
            ×
          </button>
        </span>
      </div>

      <!-- Model Checkbox List -->
      <div class="max-h-60 overflow-y-auto bg-white border border-slate-200 rounded-lg divide-y divide-slate-100">
        <div
          v-if="filteredModels.length === 0"
          class="px-3 py-6 text-center text-xs text-slate-400"
        >
          {{ loading ? '加载模型列表中…' : '无匹配模型' }}
        </div>
        <button
          v-for="m in filteredModels"
          :key="m.id"
          type="button"
          class="w-full flex items-center justify-between gap-3 px-3 py-2 text-left hover:bg-slate-50 cursor-pointer transition-colors"
          :class="{ 'bg-brand-50/40': isSelected(m.id) }"
          @click="toggleModel(m.id)"
        >
          <div class="min-w-0">
            <div class="text-xs font-medium text-slate-900 font-mono truncate">
              {{ m.id }}
            </div>
            <div class="text-[11px] text-slate-400 truncate">
              {{ m.displayName }}
            </div>
          </div>
          <span
            class="w-[18px] h-[18px] shrink-0 rounded border flex items-center justify-center transition-all"
            :class="isSelected(m.id)
              ? 'bg-brand-600 border-brand-600 text-white'
              : 'border-slate-300 text-transparent'"
          >
            <Check class="w-3 h-3" />
          </span>
        </button>
      </div>
    </div>
  </div>
</template>
