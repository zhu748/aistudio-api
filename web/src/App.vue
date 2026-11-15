<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import AppLayout from '@/components/layout/AppLayout.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'

const route = useRoute()
const router = useRouter()
// 初始导航（守卫内 /auth/check 探测）完成前不渲染 AppLayout：否则布局
// 会在鉴权状态未确认时挂载并触发数据轮询，未登录访问必然收到 401，
// 错误 toast 随后残留到登录页左上角。
const routerReady = ref(false)
void router.isReady().then(() => {
  routerReady.value = true
})
const isLoginPage = computed(() => route.meta.public === true)
</script>

<template>
  <div class="min-h-screen bg-slate-50 text-slate-900">
    <ConfirmDialog />
    <router-view v-if="isLoginPage" />
    <AppLayout v-else-if="routerReady">
      <router-view />
    </AppLayout>
  </div>
</template>
