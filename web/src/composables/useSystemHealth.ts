import { ref } from 'vue'
import { request } from '@/api/client.ts'
import { usePolling } from './usePolling.ts'

export type SystemHealthState = 'checking' | 'online' | 'degraded' | 'offline'

/**
 * 轮询 /health 接口的服务在线状态。
 * 页面不可见时自动暂停，恢复可见时自动探测。
 */
export function useSystemHealth(intervalMs = 15000) {
  const state = ref<SystemHealthState>('checking')
  const readiness = ref<{ ready: boolean; available_accounts: number; signer_ok: boolean } | null>(null)

  async function check() {
    try {
      // 必须走统一 request：裸 fetch 没有超时，服务端接受连接却不响应时
      // Promise 永不 settle，usePolling 的 running 标志会永久卡住整条轮询链。
      await request<{ status: string }>('/health', { timeoutMs: 8000 })
      try {
        readiness.value = await request('/api/ready', { timeoutMs: 6000, acceptableStatuses: [503] })
        state.value = readiness.value?.ready ? 'online' : 'degraded'
      } catch {
        state.value = 'degraded'
        readiness.value = null
      }
    } catch {
      state.value = 'offline'
    }
  }

  usePolling(check, intervalMs, { key: 'system_health', immediate: true })

  return { state, readiness }
}
