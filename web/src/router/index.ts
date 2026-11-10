import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth.ts'
const LoginView = () => import('@/views/LoginView.vue')
const DashboardView = () => import('@/views/DashboardView.vue')
const AccountsView = () => import('@/views/AccountsView.vue')
const SettingsView = () => import('@/views/SettingsView.vue')
const LogsView = () => import('@/views/LogsView.vue')

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: LoginView,
    meta: { public: true, title: '登录' },
  },
  {
    path: '/',
    name: 'dashboard',
    component: DashboardView,
    meta: { title: '控制面板' },
  },
  {
    path: '/accounts',
    name: 'accounts',
    component: AccountsView,
    meta: { title: '账号管理' },
  },
  {
    path: '/settings',
    name: 'settings',
    component: SettingsView,
    meta: { title: '系统配置' },
  },
  {
    path: '/logs',
    name: 'logs',
    component: LogsView,
    meta: { title: '运行日志' },
  },
  {
    path: '/:pathMatch(.*)*',
    redirect: '/',
  },
]

export const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.beforeEach(async (to, _from, next) => {
  // 设置文档标题
  if (to.meta.title && typeof to.meta.title === 'string') {
    document.title = `${to.meta.title} - AI Studio Proxy`
  }

  const authStore = useAuthStore()

  // 初始化鉴权状态。上一次探测失败时允许重试，但加冷却期，
  // 避免服务不可达时每次导航都同步等待一次网络超时。
  if (!authStore.initialized || (authStore.checkFailed && !to.meta.public)) {
    if (authStore.shouldRetryCheck()) {
      await authStore.checkAuth()
    }
  }

  const isPublic = to.meta.public === true

  if (authStore.authEnabled) {
    if (!authStore.token) {
      if (!isPublic) {
        return next({ name: 'login', query: { redirect: to.fullPath } })
      }
    } else {
      if (to.name === 'login') {
        return next({ name: 'dashboard' })
      }
    }
  } else if (authStore.checkFailed) {
    // 鉴权状态探测失败：无法确认服务端是否要求密码。
    // 按“未确认即拒绝”处理，避免网络异常被当成「鉴权已关闭」而放行全部受保护路由。
    if (!isPublic) {
      return next({ name: 'login', query: { redirect: to.fullPath } })
    }
  } else {
    if (to.name === 'login') {
      return next({ name: 'dashboard' })
    }
  }

  next()
})
