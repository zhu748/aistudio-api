import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { systemApi } from '@/api/system.ts'
import type { RotationStatusResponse, ClearCooldownRequest } from '@/types'
import type { RequestMetricsResponse } from '@/types/stats.ts'
import { operationsApi } from '@/api/operations.ts'
import { useToastStore } from './toast.ts'
import { errorMessage } from '@/utils/errors.ts'
import { useLatest } from '@/composables/useLatest.ts'

export const useSystemStore = defineStore('system', () => {
  const stats = ref<RequestMetricsResponse | null>(null)
  const statsRange = ref('24h')
  const customSince = ref('')
  const customUntil = ref('')
  const rotationError = ref<string | null>(null)
  const statsLatest = useLatest()
  // 与 statsLatest 同款：轮换状态会被布局/视图轮询与操作后刷新并发触发，
  // 乱序返回时旧响应不得覆盖新状态。
  const rotationLatest = useLatest()
  const rotation = ref<RotationStatusResponse | null>(null)
  const loading = ref(false)
  // 最近一次统计请求的错误；持续失败时只提示一次，避免轮询刷屏
  const statsError = ref<string | null>(null)
  const resettingCooldown = ref(false)
  const switchingNext = ref(false)

  const totalRequests = computed(() => stats.value?.totals?.requests ?? 0)
  const totalRateLimited = computed(() => stats.value?.totals?.rate_limited ?? 0)
  const totalSuccess = computed(() => stats.value?.totals?.success ?? 0)
  const totalErrors = computed(() => stats.value?.totals?.errors ?? 0)
  const totalTokens = computed(() => stats.value?.totals?.total_tokens ?? 0)
  const promptTokens = computed(() => stats.value?.totals?.prompt_tokens ?? 0)
  const completionTokens = computed(() => stats.value?.totals?.completion_tokens ?? 0)
  async function fetchStats() {
    const isLatest = statsLatest.begin()
    loading.value = true
    try {
      const now = Date.now() / 1000
      const midnight = new Date()
      midnight.setHours(0, 0, 0, 0)
      const since =
        statsRange.value === 'today'
          ? midnight.getTime() / 1000
          : statsRange.value === 'custom'
            ? new Date(customSince.value).getTime() / 1000
            : now - (statsRange.value === '7d' ? 7 : statsRange.value === '14d' ? 14 : 1) * 86400
      const until = statsRange.value === 'custom' ? new Date(customUntil.value).getTime() / 1000 : now
      if (!Number.isFinite(since) || !Number.isFinite(until) || since > until)
        throw new Error('请选择有效的开始和结束时间')
      const response = await operationsApi.metrics(
        new URLSearchParams({ since: String(since), until: String(until) }),
      )
      if (!isLatest()) return false
      stats.value = response
      statsError.value = null
      return true
    } catch (err: unknown) {
      const msg = errorMessage(err, '获取统计数据失败')
      if (isLatest()) {
        if (statsError.value === null) useToastStore().error(msg)
        statsError.value = msg
      }
      return false
    } finally {
      if (isLatest()) loading.value = false
    }
  }

  async function fetchRotation() {
    const isLatest = rotationLatest.begin()
    try {
      const response = await systemApi.getRotation()
      if (!isLatest()) return false
      rotation.value = response
      rotationError.value = null
      return true
    } catch (err: unknown) {
      const msg = errorMessage(err, '获取轮换状态失败')
      if (isLatest()) {
        if (rotationError.value === null) useToastStore().error(msg)
        rotationError.value = msg
      }
      return false
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
      const msg = errorMessage(err, '重置失败')
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
      const msg = errorMessage(err, '切换失败')
      toast.error(msg)
      return false
    } finally {
      switchingNext.value = false
    }
  }

  return {
    stats,
    statsRange,
    customSince,
    customUntil,
    rotationError,
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
    statsError,
    fetchStats,
    fetchRotation,
    clearCooldown,
    forceNextAccount,
  }
})
