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
    toast.success('抗截断配置已保存')
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
  <div class="bg-white border border-gray-200/80 rounded-2xl p-6 shadow-xs space-y-5">
    <!-- Card Header -->
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-gray-100 pb-4">
      <div class="flex items-center gap-2.5">
        <div class="p-2 rounded-xl bg-emerald-50 text-emerald-600 border border-emerald-100">
          <ShieldCheck class="w-5 h-5" />
        </div>
        <div>
          <h3 class="font-semibold text-gray-900 text-sm">
            抗截断传输
          </h3>
          <p class="text-[11px] text-gray-400 mt-0.5">
            把正文藏进工具调用参数绕开上游截断，响应侧自动还原，客户端无感知
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
      <div class="p-4 bg-gray-50/60 border border-gray-200/70 rounded-2xl space-y-2">
        <ConfigSwitch
          :model-value="globalEnabled"
          label="全局开启（所有模型）"
          description="所有模型请求默认启用抗截断，无需选择变体"
          @update:model-value="globalEnabled = $event"
        />
      </div>
      <div class="p-4 bg-gray-50/60 border border-gray-200/70 rounded-2xl space-y-2">
        <ConfigSwitch
          :model-value="variantsEnabled"
          label="模型变体模式（按模型选择）"
          description="为下方选中的模型暴露「流式抗截断 / 非流式抗截断」两个后缀变体"
          @update:model-value="variantsEnabled = $event"
        />
      </div>
    </div>

    <!-- Variant Explanation -->
    <div
      v-if="variantsEnabled"
      class="p-3.5 bg-emerald-50/70 border border-emerald-200/80 rounded-xl flex items-start gap-2.5 text-xs text-emerald-900 leading-relaxed"
    >
      <Info class="w-4 h-4 text-emerald-600 shrink-0 mt-0.5" />
      <div>
        选中模型后，模型列表会额外出现两个变体：<code class="px-1 py-0.5 rounded bg-emerald-100/80">模型名-at-stream</code>（流式抗截断：SSE 逐段增量还原）与
        <code class="px-1 py-0.5 rounded bg-emerald-100/80">模型名-at</code>（非流式抗截断：缓冲完整回复后一次性下发）。选择变体模型即启用抗截断，原模型行为不变；也可用请求头 <code>X-Anti-Truncation: 0</code> 按请求关闭。
      </div>
    </div>

    <!-- Model Multi-Select -->
    <div
      class="p-4 bg-gray-50/60 border border-gray-200/70 rounded-2xl space-y-3 transition-opacity"
      :class="{ 'opacity-50 pointer-events-none': !variantsEnabled }"
    >
      <div class="flex items-center justify-between gap-3">
        <div class="flex items-center gap-1.5 text-xs font-semibold text-gray-700">
          <Boxes class="w-3.5 h-3.5 text-brand-600" />
          <span>选择启用变体的模型</span>
        </div>
        <span
          v-if="selectedCount > 0"
          class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold bg-brand-50 text-brand-700 border border-brand-100"
        >
          已选 {{ selectedCount }} 个
        </span>
      </div>

      <!-- Search -->
      <div class="relative">
        <Search class="w-3.5 h-3.5 text-gray-400 absolute left-3 top-1/2 -translate-y-1/2" />
        <input
          v-model="searchKeyword"
          type="text"
          placeholder="搜索模型 ID 或名称..."
          class="w-full pl-9 pr-3 py-2 text-xs bg-white border border-gray-200 rounded-xl focus:outline-none focus:ring-2 focus:ring-brand-400/40 focus:border-brand-300 transition-all"
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
          class="inline-flex items-center gap-1 px-2 py-1 rounded-lg text-[11px] font-medium bg-emerald-100 text-emerald-800 border border-emerald-200"
        >
          <span class="font-mono">{{ id }}</span>
          <button
            type="button"
            class="text-emerald-500 hover:text-emerald-700 cursor-pointer"
            @click="toggleModel(id)"
          >
            ×
          </button>
        </span>
      </div>

      <!-- Model Checkbox List -->
      <div class="max-h-64 overflow-y-auto bg-white border border-gray-200 rounded-xl divide-y divide-gray-100">
        <div
          v-if="filteredModels.length === 0"
          class="px-3 py-6 text-center text-xs text-gray-400"
        >
          {{ loading ? '加载模型列表中...' : '无匹配模型' }}
        </div>
        <button
          v-for="m in filteredModels"
          :key="m.id"
          type="button"
          class="w-full flex items-center justify-between gap-3 px-3 py-2.5 text-left hover:bg-gray-50 cursor-pointer transition-colors"
          :class="{ 'bg-emerald-50/60': isSelected(m.id) }"
          @click="toggleModel(m.id)"
        >
          <div class="min-w-0">
            <div class="text-xs font-medium text-gray-900 font-mono truncate">
              {{ m.id }}
            </div>
            <div class="text-[11px] text-gray-400 truncate">
              {{ m.displayName }}
            </div>
          </div>
          <span
            class="w-4.5 h-4.5 w-[18px] h-[18px] shrink-0 rounded-md border flex items-center justify-center transition-all"
            :class="isSelected(m.id)
              ? 'bg-emerald-500 border-emerald-500 text-white'
              : 'border-gray-300 text-transparent'"
          >
            <Check class="w-3 h-3" />
          </span>
        </button>
      </div>
    </div>
  </div>
</template>
