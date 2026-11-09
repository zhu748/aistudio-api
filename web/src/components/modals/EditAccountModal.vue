<script setup lang="ts">
import { ref, watch } from 'vue'
import type { AccountWithStats } from '@/types'
import { useAccountsStore } from '@/stores/accounts.ts'
import Modal from '@/components/ui/Modal.vue'
import Button from '@/components/ui/Button.vue'

const props = defineProps<{
  modelValue: boolean
  account: AccountWithStats | null
}>()

const emit = defineEmits<{
  'update:modelValue': [val: boolean]
}>()

const accountsStore = useAccountsStore()
const inputName = ref('')
const inputProxy = ref('')

watch(
  () => props.account,
  (acc) => {
    if (acc) {
      inputName.value = acc.name || ''
      inputProxy.value = acc.proxy_url || ''
    }
  },
  { immediate: true }
)

async function handleSave() {
  if (!props.account) return
  const trimmed = inputName.value.trim()
  if (!trimmed) return

  const trimmedProxy = inputProxy.value.trim()
  const proxyChanged = trimmedProxy !== (props.account.proxy_url || '')
  if (proxyChanged) {
    await accountsStore.updateAccountProxy(props.account.id, trimmedProxy || null)
  }
  await accountsStore.updateAccountName(props.account.id, trimmed)
  emit('update:modelValue', false)
}
</script>

<template>
  <Modal
    :model-value="modelValue"
    title="编辑账号"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="space-y-4">
      <div class="space-y-1.5">
        <label class="block text-xs font-semibold text-gray-700 uppercase tracking-wider">
          账号名称 / 备注
        </label>
        <input
          v-model="inputName"
          type="text"
          placeholder="例如: 主账号 (Pro)、备用账号 1 等"
          class="w-full px-3.5 py-2.5 bg-gray-50 border border-gray-200 rounded-xl text-sm outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
        >
      </div>

      <div class="space-y-1.5">
        <label class="block text-xs font-semibold text-gray-700 uppercase tracking-wider">
          独立出口代理 <span class="text-gray-400 font-normal normal-case">(可选)</span>
        </label>
        <input
          v-model="inputProxy"
          type="text"
          placeholder="留空使用全局代理，如 socks5://127.0.0.1:1080 或 127.0.0.1:7890"
          class="w-full px-3.5 py-2.5 bg-gray-50 border border-gray-200 rounded-xl text-sm font-mono outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
        >
        <p class="text-xs text-gray-400">
          该账号的请求、Cookie 轮换与探活均走此代理；留空回退全局 AISTUDIO_PROXY
        </p>
      </div>
    </div>

    <template #footer>
      <Button
        variant="ghost"
        size="sm"
        @click="emit('update:modelValue', false)"
      >
        取消
      </Button>
      <Button
        variant="primary"
        size="sm"
        :disabled="!inputName.trim()"
        @click="handleSave"
      >
        保存修改
      </Button>
    </template>
  </Modal>
</template>
