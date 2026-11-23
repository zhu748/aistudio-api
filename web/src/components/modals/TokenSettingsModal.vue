<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth.ts'
import { useToastStore } from '@/stores/toast.ts'
import { useConfirm } from '@/composables/useConfirm.ts'
import Modal from '@/components/ui/Modal.vue'
import Button from '@/components/ui/Button.vue'
import { KeyRound, LogOut } from 'lucide-vue-next'

defineProps<{
  modelValue: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [val: boolean]
}>()

const router = useRouter()
const authStore = useAuthStore()
const toast = useToastStore()
const { confirm } = useConfirm()

// localStorage 里保存的是 /auth/session 签发的可撤销会话令牌，并非管理密码，
// 不提供手动修改入口（直接粘贴密码会导致所有请求 401），只支持退出登录。
const hasSession = computed(() => Boolean(authStore.token))

async function handleLogout() {
  const ok = await confirm({
    title: '退出当前会话',
    message: '将清除本浏览器保存的会话令牌并返回登录页，确定退出登录吗？',
    confirmText: '退出登录',
    variant: 'danger',
  })
  if (!ok) return
  authStore.logout()
  toast.info('已退出登录，本地会话已清除')
  emit('update:modelValue', false)
  void router.push('/login')
}
</script>

<template>
  <Modal
    :model-value="modelValue"
    title="登录会话管理"
    subtitle="管理当前控制台的登录会话凭据"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="space-y-4">
      <div class="flex items-start gap-2.5 p-3 bg-slate-50 rounded-lg border border-slate-200/80 text-xs text-slate-600 leading-relaxed">
        <KeyRound class="w-4 h-4 text-brand-600 shrink-0 mt-0.5" />
        <div>
          登录成功后，服务端会签发一枚与管理密码分离的随机会话令牌（30 天内免密复用、可随时撤销），保存在本浏览器并自动用于所有受保护接口与实时日志流；修改管理密码会使全部会话立即失效。会话凭据仅能通过登录页获取，不支持在此手动修改。
        </div>
      </div>

      <div class="flex items-center justify-between gap-3 px-3 py-2.5 bg-slate-50 border border-slate-200/80 rounded-lg text-xs">
        <span class="text-slate-500">当前会话状态</span>
        <span
          class="inline-flex items-center gap-1.5 font-medium"
          :class="hasSession ? 'text-emerald-700' : 'text-slate-400'"
        >
          <span
            class="w-1.5 h-1.5 rounded-full"
            :class="hasSession ? 'bg-emerald-500' : 'bg-slate-300'"
          />
          {{ hasSession ? '已登录 · 会话令牌有效' : '未登录' }}
        </span>
      </div>

      <div class="pt-2 border-t border-slate-100">
        <button
          type="button"
          class="w-full flex items-center justify-center gap-1.5 py-2 text-xs font-medium text-rose-600 hover:text-rose-700 hover:bg-rose-50 rounded-lg transition-colors cursor-pointer"
          @click="handleLogout"
        >
          <LogOut class="w-3.5 h-3.5" />
          <span>退出登录（清除本地会话）</span>
        </button>
      </div>
    </div>

    <template #footer>
      <Button
        variant="secondary"
        size="sm"
        @click="emit('update:modelValue', false)"
      >
        关闭
      </Button>
    </template>
  </Modal>
</template>
