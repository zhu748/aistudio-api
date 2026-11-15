import { request } from './client.ts'
import type { AuthCheckResponse, SessionResponse, StatsResponse } from '@/types'

export const authApi = {
  checkAuth(): Promise<AuthCheckResponse> {
    // 该请求处于路由守卫的关键路径上，超时不宜过长，
    // 否则服务不可达时整页导航会被挂起数十秒。
    return request<AuthCheckResponse>('/auth/check', { timeoutMs: 8000 })
  },

  /** 管理密码换取持久会话令牌（浏览器重开免密；密码错误返回 401）。 */
  createSession(password: string): Promise<SessionResponse> {
    return request<SessionResponse>('/auth/session', {
      method: 'POST',
      body: JSON.stringify({ password }),
    })
  },

  verifyToken(token: string): Promise<StatsResponse> {
    return request<StatsResponse>('/stats', {
      headers: {
        Authorization: `Bearer ${token}`,
      },
    })
  },
}
