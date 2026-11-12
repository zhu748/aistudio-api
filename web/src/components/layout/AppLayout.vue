<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { refreshCurrentPage } from '@/composables/usePageRefresh.ts'
import { useRoute } from 'vue-router'
import Sidebar from './Sidebar.vue'
import Topbar from './Topbar.vue'
import TokenSettingsModal from '@/components/modals/TokenSettingsModal.vue'
import ToastContainer from '@/components/ui/ToastContainer.vue'
import { useAccountsStore } from '@/stores/accounts.ts'
import { useToastStore } from '@/stores/toast.ts'

const route = useRoute()
const accountsStore = useAccountsStore()
const toast = useToastStore()

const sidebarOpen = ref(false)
const tokenModalOpen = ref(false)
const refreshing = ref(false)

const routeMetaMap: Record<string, { title: string; subtitle: string }> = {
  '/requests': { title: '请求记录', subtitle: '查询最终结果、重试链路、耗时和关联日志' },
  '/proxies': { title: '代理与出口', subtitle: '管理全局代理与账号出口，检查连通性和出口 IP' },
  '/': {
    title: '控制面板',
    subtitle: '实时监控多账号轮询状态、模型调用吞吐与配额水位',
  },
  '/accounts': {
    title: '账号凭据池',
    subtitle: '管理 Google AI Studio 会话凭据、订阅等级探测与模型 429 冷却',
  },
  '/settings': {
    title: '系统与协议配置',
    subtitle: '配置日志转储、抗截断传输、WAA 签名引擎、客户端密钥与模型映射规则',
  },
  '/logs': {
    title: '模型请求日志',
    subtitle: '查看模型调用、生成参数、耗时与 Token，按请求关联排查',
  },
}

const currentHeader = computed(() => {
  return (
    routeMetaMap[route.path] || {
      title:
        typeof route.meta.title === 'string'
          ? route.meta.title.replace(' - AI Studio Proxy', '')
          : 'AI Studio Proxy',
      subtitle: '',
    }
  )
})

async function handleRefresh() {
  if (refreshing.value) return
  refreshing.value = true
  try {
    const ok = await refreshCurrentPage()
    if (ok) toast.success('当前页面数据已刷新')
    else toast.error('部分数据刷新失败，请查看页面提示')
  } finally {
    refreshing.value = false
  }
}
onMounted(() => {
  void accountsStore.fetchAll()
})
</script>

<template>
  <div class="min-h-screen bg-slate-50 flex">
    <ToastContainer />

    <!-- Sidebar -->
    <Sidebar
      :open="sidebarOpen"
      @update:open="sidebarOpen = $event"
      @open-token-modal="tokenModalOpen = true"
    />

    <!-- Main Content Area -->
    <div class="flex-1 lg:pl-60 flex flex-col min-w-0">
      <Topbar
        :title="currentHeader.title"
        :subtitle="currentHeader.subtitle"
        :refreshing="refreshing"
        @toggle-sidebar="sidebarOpen = !sidebarOpen"
        @open-token-modal="tokenModalOpen = true"
        @refresh="handleRefresh"
      />

      <main class="flex-1 p-4 sm:p-6 max-w-7xl w-full mx-auto">
        <slot />
      </main>
    </div>

    <!-- Global Token Settings Modal -->
    <TokenSettingsModal v-model="tokenModalOpen" />
  </div>
</template>
