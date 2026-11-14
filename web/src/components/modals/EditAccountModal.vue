<script setup lang="ts">
import { ref, watch } from 'vue'
import type { AccountWithStats } from '@/types'
import { useAccountsStore } from '@/stores/accounts.ts'
import { useToastStore } from '@/stores/toast.ts'
import { operationsApi, type ProxyCheck } from '@/api/operations.ts'
import Modal from '@/components/ui/Modal.vue'
import Button from '@/components/ui/Button.vue'
import ProxyInputField from '@/components/ui/ProxyInputField.vue'
import { PlugZap } from 'lucide-vue-next'

const props = defineProps<{
  modelValue: boolean
  account: AccountWithStats | null
}>()

const emit = defineEmits<{
  'update:modelValue': [val: boolean]
}>()

const accountsStore = useAccountsStore()
const toast = useToastStore()
const inputName = ref('')
const inputProxy = ref('')
const inputNotes = ref('')
const saving = ref(false)

// Proxy connectivity test state（复用 /api/proxies/test，草稿地址不入库）
const testing = ref(false)
const checks = ref<ProxyCheck[]>([])

async function testProxy() {
  const raw = inputProxy.value.trim()
  checks.value = []
  testing.value = true
  try {
    const result = await operationsApi.testProxy(raw ? { proxy_url: raw } : {})
    checks.value = result.checks
    const failed = result.checks.filter((c) => !c.ok)
    if (failed.length) {
      toast.error(`代理连通性测试：${failed.length}/${result.checks.length} 项未通过`)
    } else {
      toast.success('代理连通性测试全部通过')
    }
  } catch (err) {
    toast.error(err instanceof Error ? err.message : '诊断失败')
  } finally {
    testing.value = false
  }
}

watch(
  () => [props.modelValue, props.account?.id],
  () => {
    const acc = props.account
    if (props.modelValue && acc) {
      inputName.value = acc.name || ''
      inputProxy.value = acc.proxy_url || ''
      inputNotes.value = acc.notes || ''
      checks.value = []
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
        <label class="text-xs font-semibold text-slate-700">
          独立出口代理 <span class="text-slate-400 font-normal">(可选)</span>
        </label>
        <div class="flex flex-wrap gap-2 items-start">
          <div class="flex-1 min-w-56">
            <ProxyInputField
              v-model="inputProxy"
              placeholder="留空使用全局代理；支持 socks5://、127.0.0.1:7890 或 turn:host:3478"
            />
          </div>
          <Button
            size="sm"
            :loading="testing"
            title="先用该代理探测 Google 连通性与出口 IP，通过后再保存"
            @click="testProxy"
          >
            <PlugZap class="w-3.5 h-3.5" />
            <span>测试连通</span>
          </Button>
        </div>
        <p class="text-[11px] text-slate-400 leading-relaxed">
          该账号的模型请求、Cookie 续期与等级探测均走此独立代理；留空则自动回退至全局
          <code class="font-mono">AISTUDIO_PROXY</code>（测试连通留空时同样走全局出口）。
        </p>
        <div
          v-if="testing || checks.length"
          class="grid grid-cols-1 sm:grid-cols-3 gap-2"
        >
          <div
            v-for="check in checks"
            :key="check.name"
            class="rounded-lg border p-2.5 text-[11px] space-y-1"
            :class="check.ok ? 'border-emerald-200 bg-emerald-50' : 'border-rose-200 bg-rose-50'"
          >
            <p class="font-semibold">
              {{ check.name }} · {{ check.ok ? '通过' : '失败' }}
            </p>
            <p>
              {{ check.elapsed_ms }}ms <span v-if="check.status_code">· HTTP {{ check.status_code }}</span>
            </p>
            <p
              v-if="check.ip"
              class="font-mono"
            >
              {{ check.ip }}
            </p>
            <p
              v-if="check.error"
              class="break-all text-rose-700"
            >
              {{ check.error }}
            </p>
          </div>
          <p
            v-if="testing"
            class="col-span-full text-[11px] text-slate-400"
          >
            正在检测 Google 连通性与出口 IP…
          </p>
        </div>
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
