<script setup lang="ts">
import { ref, watch } from 'vue'
import { useAuthStore } from '@/stores/auth.ts'
import { useToastStore } from '@/stores/toast.ts'
import { useConfirm } from '@/composables/useConfirm.ts'
import Modal from '@/components/ui/Modal.vue'
import Button from '@/components/ui/Button.vue'
import { KeyRound, Trash2 } from 'lucide-vue-next'

const props = defineProps<{
  modelValue: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [val: boolean]
}>()

const authStore = useAuthStore()
const toast = useToastStore()
const { confirm } = useConfirm()

const inputToken = ref(authStore.token)

watch(
  () => props.modelValue,
  (val) => {
    if (val) {
      inputToken.value = authStore.token
    }
  }
)

function handleSave() {
  authStore.updateToken(inputToken.value)
  toast.success('控制台访问密码已更新')
  emit('update:modelValue', false)
}

async function handleClearCache() {
  const ok = await confirm({
    title: '清除本地鉴权缓存',
    message: '确定要清除浏览器本地保存的管理密码与登录状态吗？清除后将重新加载页面。',
    confirmText: '确认清除',
    variant: 'danger',
  })
  if (ok) {
    authStore.logout()
    inputToken.value = ''
    toast.info('本地鉴权缓存已清除')
    emit('update:modelValue', false)
    window.location.reload()
  }
}
</script>

<template>
  <Modal
    :model-value="modelValue"
    title="控制台管理密码设置"
    subtitle="用于校验管理后台接口与实时日志流权限"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="space-y-4">
      <div class="flex items-start gap-2.5 p-3 bg-slate-50 rounded-lg border border-slate-200/80 text-xs text-slate-600 leading-relaxed">
        <KeyRound class="w-4 h-4 text-brand-600 shrink-0 mt-0.5" />
        <div>
          服务端启用 <code class="font-mono text-slate-800 bg-white px-1 py-0.5 rounded border border-slate-200">AISTUDIO_WEB_PASSWORD</code> 后，控制台会自动携带此密钥访问受保护接口。
        </div>
      </div>

      <div class="space-y-1.5">
        <label class="block text-xs font-semibold text-slate-700">
          管理密码 (Web Password)
        </label>
        <input
          v-model="inputToken"
          type="password"
          placeholder="留空则不发送 Authorization 请求头"
          class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg text-xs font-mono outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
        >
      </div>

      <div class="pt-2 border-t border-slate-100">
        <button
          type="button"
          class="w-full flex items-center justify-center gap-1.5 py-2 text-xs font-medium text-rose-600 hover:text-rose-700 hover:bg-rose-50 rounded-lg transition-colors cursor-pointer"
          @click="handleClearCache"
        >
          <Trash2 class="w-3.5 h-3.5" />
          <span>清除本地保存的密码与缓存</span>
        </button>
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
        @click="handleSave"
      >
        保存设置
      </Button>
    </template>
  </Modal>
</template>
