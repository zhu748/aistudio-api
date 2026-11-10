import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { accountsApi } from '@/api/accounts.ts'
import { useSystemStore } from './system.ts'
import type {
  Account,
  AccountWithStats,
  AccountRotationStats,
  ImportCookiesRequest,
  ProbeAndImportRequest,
  ImportBundleRequest,
  AccountPreset,
} from '@/types'
import { useToastStore } from './toast.ts'

export const useAccountsStore = defineStore('accounts', () => {
  const accounts = ref<Account[]>([])
  const activeAccount = ref<Account | null>(null)
  const systemStore = useSystemStore()
  const rotationAccounts = computed<Record<string, AccountRotationStats>>(() => {
    return systemStore.rotation?.accounts || {}
  })
  const loading = ref(false)
  const activatingId = ref<string | null>(null)
  const importing = ref(false)
  const presets = ref<AccountPreset[]>([])
  const groups = ref<string[]>([])
  const applyingPresetId = ref<string | null>(null)
  const activeId = computed(() => activeAccount.value?.id || '')

  // 按 id 复用行对象引用（P8）：轮询每 8s 全量替换数组时，若账号数据与其
  // 调度统计均未变化，复用上一次的对象引用，避免几百账号 × 每账号模型
  // 配额网格的整棵组件树全量重渲染。字段值均为原始类型，浅比较即可。
  const rowCache = new Map<string, AccountWithStats>()

  function sameFields(fresh: Record<string, unknown>, prev: Record<string, unknown>): boolean {
    for (const k of Object.keys(fresh)) {
      if (fresh[k] !== prev[k]) return false
    }
    return true
  }

  const accountRows = computed<AccountWithStats[]>(() => {
    const nextCache = new Map<string, AccountWithStats>()
    const rows = accounts.value.map((a) => {
      const rot = rotationAccounts.value[a.id] || {}
      const rotFields = rot as unknown as Record<string, unknown>
      const prev = rowCache.get(a.id)
      const prevFields = prev as unknown as Record<string, unknown>
      if (prev && sameFields(a as unknown as Record<string, unknown>, prevFields) && sameFields(rotFields, prevFields)) {
        nextCache.set(a.id, prev)
        return prev
      }
      const row = { ...a, ...rot } as AccountWithStats
      nextCache.set(a.id, row)
      return row
    })
    rowCache.clear()
    for (const [k, v] of nextCache) rowCache.set(k, v)
    return rows
  })

  // 请求代次：轮询与操作后的 fetchAll 可能乱序返回，只让最新一次写状态。
  let fetchSeq = 0

  async function fetchAll() {
    const seq = ++fetchSeq
    loading.value = true
    try {
      const [accsRes, activeRes] = await Promise.allSettled([
        accountsApi.list(),
        accountsApi.getActive(),
        systemStore.fetchRotation(),
      ])
      if (seq !== fetchSeq) return

      if (accsRes.status === 'fulfilled') {
        accounts.value = accsRes.value
      } else {
        const reasonMsg = accsRes.reason instanceof Error ? accsRes.reason.message : String(accsRes.reason)
        console.warn('获取账号列表失败，保留当前展示数据:', accsRes.reason)
        useToastStore().error(`获取账号列表失败: ${reasonMsg}`)
      }

      if (activeRes.status === 'fulfilled') {
        activeAccount.value = activeRes.value
      } else {
        // 瞬时失败不代表没有激活账号：保留旧值，避免面板谎报「无激活账号」
        console.warn('获取激活账号失败，保留当前展示数据:', activeRes.reason)
      }
    } finally {
      if (seq === fetchSeq) loading.value = false
    }
  }

  async function fetchPresets() {
    try {
      presets.value = await accountsApi.listPresets()
    } catch {
      // ignore
    }
  }

  async function fetchGroups() {
    try {
      groups.value = await accountsApi.listGroups()
    } catch {
      // ignore
    }
  }

  async function toggleAccountEnabled(id: string, enabled?: boolean) {
    const toast = useToastStore()
    try {
      const updated = await accountsApi.toggle(id, enabled)
      toast.success(`账号已${updated.enabled ? '启用' : '停用'}`)
      await fetchAll()
      return updated
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '切换状态失败'
      toast.error(msg)
      return null
    }
  }

  async function updateAccountGroup(id: string, group: string) {
    const toast = useToastStore()
    try {
      const updated = await accountsApi.updateGroup(id, group)
      toast.success('分组已更新')
      await fetchAll()
      await fetchGroups()
      return updated
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '更新分组失败'
      toast.error(msg)
      return null
    }
  }

  async function applyPreset(presetId: string) {
    const toast = useToastStore()
    applyingPresetId.value = presetId
    try {
      const res = await accountsApi.applyPreset(presetId)
      toast.success(`已应用「${res.preset_name}」: 启用 ${res.enabled_count} 个`)
      await fetchAll()
      return true
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '应用方案失败'
      toast.error(msg)
      return false
    } finally {
      applyingPresetId.value = null
    }
  }

  async function createSnapshotPreset(name: string, description = '') {
    const toast = useToastStore()
    try {
      const created = await accountsApi.createSnapshotPreset({ name, description })
      toast.success(`方案「${created.name}」已保存`)
      await fetchPresets()
      return created
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '保存方案失败'
      toast.error(msg)
      return null
    }
  }

  async function deletePreset(presetId: string) {
    const toast = useToastStore()
    try {
      await accountsApi.deletePreset(presetId)
      toast.success('方案已删除')
      await fetchPresets()
      return true
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '删除方案失败'
      toast.error(msg)
      return false
    }
  }

  async function activateAccount(id: string) {
    const toast = useToastStore()
    activatingId.value = id
    try {
      const updated = await accountsApi.activate(id)
      activeAccount.value = updated
      toast.success(`已成功激活账号: ${updated.name || updated.email || updated.id}`)
      await fetchAll()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '激活失败'
      toast.error(msg)
      await fetchAll()
    } finally {
      activatingId.value = null
    }
  }

  async function deleteAccount(id: string) {
    const toast = useToastStore()
    try {
      await accountsApi.delete(id)
      toast.success('账号已删除')
      await fetchAll()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '删除失败'
      toast.error(msg)
    }
  }

  async function deleteCookieGroup(cookieId: string) {
    const toast = useToastStore()
    try {
      const res = await accountsApi.deleteCookieGroup(cookieId)
      toast.success(`已成功删除该 Cookie 凭据下的全部 ${res.deleted} 个子账号`)
      await fetchAll()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '删除失败'
      toast.error(msg)
    }
  }

  async function updateAccountName(id: string, name: string) {
    const toast = useToastStore()
    try {
      await accountsApi.update(id, name)
      toast.success('账号名称已更新')
      await fetchAll()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '更新失败'
      toast.error(msg)
    }
  }

  async function updateAccountProxy(id: string, proxyUrl: string | null) {
    const toast = useToastStore()
    try {
      await accountsApi.updateProxy(id, proxyUrl)
      toast.success(proxyUrl ? `账号代理已更新: ${proxyUrl}` : '已恢复使用全局代理')
      await fetchAll()
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '代理更新失败'
      toast.error(msg)
    }
  }

  async function importCookies(payload: ImportCookiesRequest) {
    const toast = useToastStore()
    importing.value = true
    try {
      const res = await accountsApi.importCookies(payload)
      toast.success(`导入成功: 解析出 ${res.cookie_count} 个 Cookie (u/${res.auth_user})`)
      await fetchAll()
      return true
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '导入失败'
      toast.error(msg)
      return false
    } finally {
      importing.value = false
    }
  }

  async function probeAndImport(payload: ProbeAndImportRequest) {
    const toast = useToastStore()
    importing.value = true
    try {
      const res = await accountsApi.probeAndImport(payload)
      toast.success(`已完成探测并导入 ${res.imported_count} 个账号`)
      await fetchAll()
      return true
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '自动探活导入失败'
      toast.error(msg)
      return false
    } finally {
      importing.value = false
    }
  }

  async function importBundle(payload: ImportBundleRequest) {
    const toast = useToastStore()
    importing.value = true
    try {
      const res = await accountsApi.importBundle(payload)
      toast.success(`批量导入成功: 已入库 ${res.imported_count} 个账号`)
      await fetchAll()
      return true
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '批量导入失败'
      toast.error(msg)
      return false
    } finally {
      importing.value = false
    }
  }

  async function detectAccountEmail(id: string) {
    const toast = useToastStore()
    try {
      const res = await accountsApi.detectEmail(id)
      const tierSuffix = res.tier ? ` · 等级: ${res.tier}` : ''
      if (res.email) {
        toast.success(`已成功识别账号邮箱: ${res.email}${tierSuffix}`)
      } else {
        toast.info(`未在当前页面识别到有效邮箱${tierSuffix}`)
      }
      await fetchAll()
      return res
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '识别邮箱失败'
      toast.error(msg)
      return null
    }
  }

  async function detectAccountTier(id: string) {
    const toast = useToastStore()
    try {
      const res = await accountsApi.detectTier(id)
      toast.success(`已更新账号订阅等级: ${res.tier || 'Free'}`)
      await fetchAll()
      return res
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : '探测订阅等级失败'
      toast.error(msg)
      return null
    }
  }

  return {
    accounts,
    activeAccount,
    activeId,
    rotationAccounts,
    accountRows,
    loading,
    activatingId,
    importing,
    fetchAll,
    activateAccount,
    deleteAccount,
    deleteCookieGroup,
    updateAccountName,
    updateAccountProxy,
    importCookies,
    probeAndImport,
    importBundle,
    detectAccountEmail,
    detectAccountTier,
    presets,
    groups,
    applyingPresetId,
    fetchPresets,
    fetchGroups,
    toggleAccountEnabled,
    updateAccountGroup,
    applyPreset,
    createSnapshotPreset,
    deletePreset,
  }
})
