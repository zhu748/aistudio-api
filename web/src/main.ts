import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { router } from './router/index.ts'
import { setUnauthorizedHandler } from './api/client.ts'
import { useAuthStore } from './stores/auth.ts'
import './assets/main.css'

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)

// 401 统一处理：清除本地 token 并走站内路由跳转到登录页。
// 不走 window.location.href 整页刷新——那会丢掉 SPA 状态，
// 且在子路径部署（如 /proxy/）时硬编码 /login 会 404。
setUnauthorizedHandler(() => {
  const authStore = useAuthStore(pinia)
  authStore.logout()
  const current = router.currentRoute.value
  if (current.name === 'login') return
  void router.replace({
    name: 'login',
    query: { redirect: current.fullPath },
  })
})

app.mount('#app')
