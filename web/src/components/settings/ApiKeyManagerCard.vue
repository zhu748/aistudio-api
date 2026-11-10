<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { systemApi } from '@/api/system.ts'
import { useToastStore } from '@/stores/toast.ts'
import { useConfirm } from '@/composables/useConfirm.ts'
import type { ApiKeyItem } from '@/types'
import Button from '@/components/ui/Button.vue'
import Modal from '@/components/ui/Modal.vue'
import { useClipboard } from '@/composables/useClipboard.ts'
import {
  Key,
  Plus,
  Copy,
  Check,
  Eye,
  EyeOff,
  Trash2,
  Edit2,
  ShieldCheck,
  AlertTriangle,
} from 'lucide-vue-next'

const toast = useToastStore()
const { confirm } = useConfirm()

const apiKeys = ref<ApiKeyItem[]>([])
const loading = ref(false)
const visibleKeys = ref<Set<string>>(new Set())
const { copy, copied } = useClipboard()

// Create Key Modal
const createModalOpen = ref(false)
const creating = ref(false)
const newKeyName = ref('')
const customKeyValue = ref('')

// Edit Key Name Modal
const editModalOpen = ref(false)
const editingKey = ref<ApiKeyItem | null>(null)
const editKeyNameInput = ref('')

async function loadApiKeys() {
  loading.value = true
  try {
    apiKeys.value = await systemApi.listApiKeys()
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '获取 API Key 列表失败'
    toast.error(msg)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void loadApiKeys()
})

function toggleVisibility(key: string) {
  if (visibleKeys.value.has(key)) {
    visibleKeys.value.delete(key)
  } else {
    visibleKeys.value.add(key)
  }
}

function maskKey(key: string): string {
  if (key.length <= 12) return '••••••••••••'
  return `${key.slice(0, 8)}••••••••${key.slice(-4)}`
}

function handleCopy(key: string) {
  void copy(key, key, '已复制 API Key 到剪贴板')
}

function openCreateModal() {
  newKeyName.value = ''
  customKeyValue.value = ''
  createModalOpen.value = true
}

async function handleCreateKey() {
  creating.value = true
  try {
    const created = await systemApi.createApiKey({
      name: newKeyName.value.trim() || 'API Key',
      key: customKeyValue.value.trim() || undefined,
    })
    toast.success(`成功创建 API Key: ${created.name}`)
    createModalOpen.value = false
    await loadApiKeys()
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '创建 API Key 失败'
    toast.error(msg)
  } finally {
    creating.value = false
  }
}

function openEditModal(item: ApiKeyItem) {
  editingKey.value = item
  editKeyNameInput.value = item.name
  editModalOpen.value = true
}

async function handleSaveEditName() {
  if (!editingKey.value) return
  const name = editKeyNameInput.value.trim()
  if (!name) return

  try {
    await systemApi.updateApiKeyName(editingKey.value.key, { name })
    toast.success('API Key 备注名已更新')
    editModalOpen.value = false
    await loadApiKeys()
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '更新备注失败'
    toast.error(msg)
  }
}

async function handleDeleteKey(item: ApiKeyItem) {
  const ok = await confirm({
    title: '吊销客户端 API Key',
    message: `确定要删除密钥「${item.name}」吗？删除后使用该 Key 的客户端将无法继续调用接口。`,
    confirmText: '确认删除',
    variant: 'danger',
  })
  if (ok) {
    try {
      await systemApi.deleteApiKey(item.key)
      toast.success('API Key 已删除')
      await loadApiKeys()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '删除失败'
      toast.error(msg)
    }
  }
}
</script>

<template>
  <div class="bg-white border border-slate-200/90 rounded-xl p-5 shadow-2xs space-y-4">
    <!-- Header -->
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-100 pb-4">
      <div class="flex items-center gap-2.5">
        <div class="w-8 h-8 rounded-lg bg-slate-900 text-brand-400 flex items-center justify-center shrink-0">
          <Key class="w-4 h-4" />
        </div>
        <div>
          <div class="flex items-center gap-2">
            <h3 class="font-semibold text-slate-900 text-sm">
              客户端调用密钥 (Client API Keys)
            </h3>
            <span class="px-2 py-0.5 text-[11px] font-mono tabular-nums bg-slate-100 text-slate-600 rounded">
              {{ apiKeys.length }} 个密钥
            </span>
          </div>
          <p class="text-[11px] text-slate-400 mt-0.5">
            同时适用于 Gemini (<code class="font-mono">x-goog-api-key</code>)、OpenAI (<code class="font-mono">Bearer</code>) 与 Claude (<code class="font-mono">x-api-key</code>) 协议鉴权
          </p>
        </div>
      </div>

      <div>
        <Button
          variant="primary"
          size="sm"
          @click="openCreateModal"
        >
          <Plus class="w-3.5 h-3.5" />
          <span>创建 API Key</span>
        </Button>
      </div>
    </div>

    <!-- Status Banner -->
    <div
      v-if="!apiKeys.length"
      class="p-3.5 bg-amber-50/70 border border-amber-200/80 rounded-lg text-xs text-amber-900 flex items-start gap-2.5"
    >
      <AlertTriangle class="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
      <div>
        当前未配置任何客户端 API Key，代理端点处于开放访问状态。添加首个密钥后将立即对所有入站调用强制鉴权。
      </div>
    </div>

    <!-- Key List Table -->
    <div
      v-if="apiKeys.length"
      class="overflow-x-auto border border-slate-200/80 rounded-lg"
    >
      <table class="w-full text-left border-collapse text-xs">
        <thead>
          <tr class="bg-slate-50 text-slate-500 text-[11px] uppercase tracking-wider font-semibold border-b border-slate-200/70">
            <th class="py-2.5 px-4">
              备注名称
            </th>
            <th class="py-2.5 px-4">
              API 密钥 (Token)
            </th>
            <th class="py-2.5 px-4">
              创建时间
            </th>
            <th class="py-2.5 px-4 text-right">
              操作
            </th>
          </tr>
        </thead>
        <tbody class="divide-y divide-slate-100">
          <tr
            v-for="item in apiKeys"
            :key="item.key"
            class="hover:bg-slate-50/60 transition-colors"
          >
            <!-- Name / Memo -->
            <td class="py-3 px-4">
              <div class="flex items-center gap-1.5 font-semibold text-slate-900">
                <span>{{ item.name }}</span>
                <button
                  type="button"
                  class="text-slate-300 hover:text-slate-600 p-0.5 rounded cursor-pointer"
                  title="修改备注名"
                  @click="openEditModal(item)"
                >
                  <Edit2 class="w-3 h-3" />
                </button>
              </div>
            </td>

            <!-- Secret Key with Visibility -->
            <td class="py-3 px-4 font-mono">
              <div class="flex items-center gap-1.5">
                <span class="text-slate-800 bg-slate-50 px-2 py-0.5 rounded border border-slate-200/80">
                  {{ visibleKeys.has(item.key) ? item.key : maskKey(item.key) }}
                </span>

                <button
                  type="button"
                  class="text-slate-400 hover:text-slate-700 p-1 rounded hover:bg-slate-100 cursor-pointer"
                  :title="visibleKeys.has(item.key) ? '隐藏密钥' : '显示完整密钥'"
                  @click="toggleVisibility(item.key)"
                >
                  <EyeOff
                    v-if="visibleKeys.has(item.key)"
                    class="w-3.5 h-3.5"
                  />
                  <Eye
                    v-else
                    class="w-3.5 h-3.5"
                  />
                </button>

                <button
                  type="button"
                  class="text-slate-400 hover:text-brand-600 p-1 rounded hover:bg-brand-50 cursor-pointer"
                  title="复制密钥"
                  @click="handleCopy(item.key)"
                >
                  <Check
                    v-if="copied === item.key"
                    class="w-3.5 h-3.5 text-emerald-500"
                  />
                  <Copy
                    v-else
                    class="w-3.5 h-3.5"
                  />
                </button>
              </div>
            </td>

            <!-- Created At -->
            <td class="py-3 px-4 text-slate-400 font-mono tabular-nums">
              {{ item.created_at || '配置文件载入' }}
            </td>

            <!-- Actions -->
            <td class="py-3 px-4 text-right">
              <button
                type="button"
                class="text-slate-400 hover:text-rose-600 p-1 rounded-md hover:bg-rose-50 transition-colors cursor-pointer"
                title="删除 API Key"
                @click="handleDeleteKey(item)"
              >
                <Trash2 class="w-3.5 h-3.5" />
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Create Modal -->
    <Modal
      v-model="createModalOpen"
      max-width="sm"
      title="创建客户端 API Key"
      subtitle="生成的密钥可同时用于 Gemini、OpenAI 与 Claude 兼容接口"
    >
      <div class="space-y-3.5">
        <div class="space-y-1.5">
          <label class="block text-xs font-semibold text-slate-700">
            备注名称 / 客户端标识
          </label>
          <input
            v-model="newKeyName"
            type="text"
            placeholder="例如: claude-code、cursor-ide、prod-app"
            class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg text-xs outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
          >
        </div>

        <div class="space-y-1.5">
          <label class="block text-xs font-semibold text-slate-700">
            自定义密钥值 <span class="text-slate-400 font-normal">(留空自动生成随机 sk- 密钥)</span>
          </label>
          <input
            v-model="customKeyValue"
            type="text"
            placeholder="sk-..."
            class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg text-xs font-mono outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
          >
        </div>
      </div>

      <template #footer>
        <Button
          variant="ghost"
          size="sm"
          :disabled="creating"
          @click="createModalOpen = false"
        >
          取消
        </Button>
        <Button
          variant="primary"
          size="sm"
          :loading="creating"
          @click="handleCreateKey"
        >
          <ShieldCheck class="w-3.5 h-3.5" />
          <span>确认创建</span>
        </Button>
      </template>
    </Modal>

    <!-- Edit Name Modal -->
    <Modal
      v-model="editModalOpen"
      max-width="sm"
      title="修改 API Key 备注名"
    >
      <div class="space-y-3">
        <div class="space-y-1.5">
          <label class="block text-xs font-semibold text-slate-700">
            备注名称
          </label>
          <input
            v-model="editKeyNameInput"
            type="text"
            class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg text-xs outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
          >
        </div>
      </div>

      <template #footer>
        <Button
          variant="ghost"
          size="sm"
          @click="editModalOpen = false"
        >
          取消
        </Button>
        <Button
          variant="primary"
          size="sm"
          :disabled="!editKeyNameInput.trim()"
          @click="handleSaveEditName"
        >
          保存修改
        </Button>
      </template>
    </Modal>
  </div>
</template>
