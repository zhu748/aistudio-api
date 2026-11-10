import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { systemApi } from '@/api/system.ts'
import type { StatsResponse, RotationStatusResponse, ClearCooldownRequest } from '@/types'
import { useToastStore } from './toast.ts'

export const useSystemStore = defineStore('system', () => {
  const stats = ref<StatsResponse | null>(null)
  const rotation = ref<RotationStatusResponse | null>(null)
  const loading = ref(false)
  // 最近一次统计请求的错误；持续失败时只提示一次，避免轮询刷屏
  const statsError = ref<string | null>(null)
  const resettingCooldown = ref(false)
  const switchingNext = ref(false)
  const health = ref<'checking' | 'online' | 'offline'>('checking')

  const totalRequests = computed(() => stats.value?.totals?.requests ?? 0)
  const totalRateLimited = computed(() => stats.value?.totals?.rate_limited ?? 0)
  const totalSuccess = computed(() => stats.value?.totals?.success ?? 0)
  const totalErrors = computed(() => stats.value?.totals?.errors ?? 0)
  const totalTokens = computed(() => stats.value?.totals?.total_tokens ?? 0)
  const promptTokens = computed(() => stats.value?.totals?.prompt_tokens ?? 0)
  const completionTokens = computed(() => stats.value?.totals?.completion_tokens ?? 0)
  async function fetchStats() {
    loading.value = true
    try {
      stats.value = await systemApi.getStats()
      statsError.value = null
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '获取统计数据失败'
      if (statsError.value === null) useToastStore().error(msg)
      statsError.value = msg
    } finally {
      loading.value = false
    }
  }

  async function fetchRotation() {
    try {
      rotation.value = await systemApi.getRotation()
      statsError.value = null
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '获取轮换状态失败'
      if (statsError.value === null) useToastStore().error(msg)
      statsError.value = msg
    }
  }
  async function checkHealth() {
    try {
      await systemApi.health()
      health.value = 'online'
    } catch {
      health.value = 'offline'
    }
  }


  async function clearCooldown(req: ClearCooldownRequest = {}) {
    const toast = useToastStore()
    resettingCooldown.value = true
    try {
      await systemApi.clearCooldown(req)
      toast.success(req.account_id ? '账号配额锁定已清除' : '所有账号配额锁定已重置')
      await fetchRotation()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '重置失败'
      toast.error(msg)
    } finally {
      resettingCooldown.value = false
    }
  }

  async function forceNextAccount() {
    const toast = useToastStore()
    switchingNext.value = true
    try {
      const res = await systemApi.forceNextAccount()
      toast.success(`已切换至账号: ${res.account.name || res.account.email || res.account.id}`)
      await fetchRotation()
      return true
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '切换失败'
      toast.error(msg)
      return false
    } finally {
      switchingNext.value = false
    }
  }

  return {
    stats,
    rotation,
    loading,
    resettingCooldown,
    switchingNext,
    totalRequests,
    totalRateLimited,
    totalSuccess,
    totalErrors,
    totalTokens,
    promptTokens,
    completionTokens,
    health,
    statsError,
    fetchStats,
    fetchRotation,
    clearCooldown,
    forceNextAccount,
    checkHealth,
  }
})
