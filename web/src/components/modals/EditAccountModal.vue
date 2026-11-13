<script setup lang="ts">
import { ref, watch } from 'vue'
import type { AccountWithStats } from '@/types'
import { useAccountsStore } from '@/stores/accounts.ts'
import Modal from '@/components/ui/Modal.vue'
import Button from '@/components/ui/Button.vue'
import ProxyInputField from '@/components/ui/ProxyInputField.vue'

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
const inputNotes = ref('')
const saving = ref(false)

watch(
  () => [props.modelValue, props.account?.id],
  () => {
    const acc = props.account
    if (props.modelValue && acc) {
      inputName.value = acc.name || ''
      inputProxy.value = acc.proxy_url || ''
      inputNotes.value = acc.notes || ''
    }
  },
  { immediate: true },
)

async function handleSave() {
  if (!props.account || saving.value) return
  const trimmed = inputName.value.trim()
  if (!trimmed) return

  const trimmedProxy = inputProxy.value.trim()
  const proxyChanged = trimmedProxy !== (props.account.proxy_url || '')
  saving.value = true
  try {
    if (proxyChanged && !(await accountsStore.updateAccountProxy(props.account.id, trimmedProxy || null)))
      return
    if (
      inputNotes.value !== (props.account.notes || '') &&
      !(await accountsStore.updateAccountNotes(props.account.id, inputNotes.value))
    )
      return
    if (trimmed !== props.account.name && !(await accountsStore.updateAccountName(props.account.id, trimmed)))
      return
    emit('update:modelValue', false)
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <Modal
    :model-value="modelValue"
    title="编辑账号信息与代理"
    :subtitle="account ? `账号 ID: ${account.id} (u/${account.auth_user})` : ''"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="space-y-4">
      <div class="space-y-1.5">
        <label class="block text-xs font-semibold text-slate-700"> 账号备注名称 </label>
        <input
          v-model="inputName"
          type="text"
          placeholder="例如: 主力账号 (Pro)、备用节点 1"
          class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg text-xs outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
        >
      </div>

      <div class="space-y-1.5">
        <label class="block text-xs font-semibold text-slate-700">
          独立出口代理 <span class="text-slate-400 font-normal">(可选)</span>
        </label>
        <ProxyInputField
          v-model="inputProxy"
          placeholder="留空使用全局代理；支持 socks5://、127.0.0.1:7890 或 turn:host:3478"
        />
        <p class="text-[11px] text-slate-400 leading-relaxed">
          该账号的模型请求、Cookie 续期与等级探测均走此独立代理；留空则自动回退至全局
          <code class="font-mono">AISTUDIO_PROXY</code>。
        </p>
      </div>
      <label class="field-label">账号备注<textarea
        v-model="inputNotes"
        rows="3"
        class="field-input resize-y"
        placeholder="账号用途、维护说明等"
      />
      </label>
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
        :loading="saving"
        @click="handleSave"
      >
        保存修改
      </Button>
    </template>
  </Modal>
</template>
