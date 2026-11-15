import { defineStore } from 'pinia'
import { ref } from 'vue'
import { authApi } from '@/api/auth.ts'
import { getToken, setToken, clearToken } from '@/api/client.ts'

export const useAuthStore = defineStore('auth', () => {
  const authEnabled = ref(false)
  const token = ref(getToken())
  const checking = ref(false)
  const initialized = ref(false)
  // 鉴权状态探测失败（网络异常 / 服务不可达）。
  // 此时绝不能把「未开启鉴权」当成结论放行受保护路由，
  // 否则服务端一旦配置了密码，探测失败就等于完全绕过登录。
  const checkFailed = ref(false)
  // 上一次探测失败的时间戳（毫秒），用于导航重试的冷却
  let lastCheckFailureAt = 0
  const CHECK_RETRY_COOLDOWN_MS = 5000

  /** 探测失败后是否已过冷却期（避免每次导航都阻塞在网络超时上）。 */
  function shouldRetryCheck(): boolean {
    if (!checkFailed.value) return true
    return Date.now() - lastCheckFailureAt >= CHECK_RETRY_COOLDOWN_MS
  }

  async function checkAuth(): Promise<boolean> {
    checking.value = true
    checkFailed.value = false
    try {
      const res = await authApi.checkAuth()
      authEnabled.value = res.auth_enabled
      if (!res.auth_enabled) {
        return true
      }

      if (!token.value) {
        return false
      }

      try {
        await authApi.verifyToken(token.value)
        return true
      } catch {
        clearToken()
        token.value = ''
        return false
      }
    } catch {
      // 只有在明确拿到 /auth/check 响应时才能判定鉴权是否开启；
      // 请求失败时保持 authEnabled 原值并把状态标记为「探测失败」，
      // 由路由守卫按“未确认即拒绝”处理。
      checkFailed.value = true
      lastCheckFailureAt = Date.now()
      return false
    } finally {
      checking.value = false
      initialized.value = true
    }
  }

  async function login(password: string): Promise<boolean> {
    const trimmed = password.trim()
    if (!trimmed) return false

    try {
      // 用密码换取持久会话令牌：localStorage 存的是随机会话凭据（30 天
      // 过期、换密码即失效），而非管理密码本身。401 即密码错误。
      const session = await authApi.createSession(trimmed)
      setToken(session.token)
      token.value = session.token
      checkFailed.value = false
      // 登录成功即证明服务端开启了鉴权：此前 /auth/check 网络失败时
      // authEnabled 会停留在 false，导致退出按钮隐藏、日志流不取票据。
      authEnabled.value = true
      return true
    } catch {
      return false
    }
  }

  /** 恢复已持久化的会话令牌（浏览器重开后的免密登录路径）。 */
  async function restoreSession(): Promise<boolean> {
    if (!token.value) return false
    try {
      await authApi.verifyToken(token.value)
      authEnabled.value = true
      checkFailed.value = false
      return true
    } catch {
      // 令牌过期/被撤销/换过密码：清掉回落到登录页
      clearToken()
      token.value = ''
      return false
    }
  }

  function logout() {
    clearToken()
    token.value = ''
  }

  function updateToken(newToken: string) {
    const trimmed = newToken.trim()
    setToken(trimmed)
    token.value = trimmed
  }

  return {
    authEnabled,
    token,
    checking,
    initialized,
    checkFailed,
    checkAuth,
    shouldRetryCheck,
    login,
    restoreSession,
    logout,
    updateToken,
  }
})
