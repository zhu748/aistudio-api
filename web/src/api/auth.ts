import { request } from './client.ts'
import type { AuthCheckResponse, StatsResponse } from '@/types'

export const authApi = {
  checkAuth(): Promise<AuthCheckResponse> {
    // 该请求处于路由守卫的关键路径上，超时不宜过长，
    // 否则服务不可达时整页导航会被挂起数十秒。
    return request<AuthCheckResponse>('/auth/check', { timeoutMs: 8000 })
  },

  verifyToken(token: string): Promise<StatsResponse> {
    return request<StatsResponse>('/stats', {
      headers: {
        Authorization: `Bearer ${token}`,
      },
    })
  },
}
