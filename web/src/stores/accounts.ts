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
import { errorMessage } from '@/utils/errors.ts'
import { useLatest } from '@/composables/useLatest.ts'

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
  // 配额网格的整棵组件树全量重渲染。5 个嵌套统计字段按值递归比较，其余浅比较。
  const rowCache = new Map<string, AccountWithStats>()

  // rotation 响应每次轮询都是新对象：其中 5 个嵌套 Record 字段必须按值
  // 递归比较（限深 3 层：today_models 为 模型 → 字段 → 原始值），否则
  // 引用不等会让行缓存永不命中。
  const NESTED_ROW_FIELDS = [
    'model_requests',
    'model_rate_limited',
    'model_drip_mode',
    'model_cooldowns',
    'today_models',
  ]

  function sameValue(a: unknown, b: unknown, depth: number): boolean {
    if (a === b) return true
    if (depth <= 0 || typeof a !== 'object' || typeof b !== 'object' || a === null || b === null) {
      return false
    }
    const ra = a as Record<string, unknown>
    const rb = b as Record<string, unknown>
    const ka = Object.keys(ra)
    if (ka.length !== Object.keys(rb).length) return false
    for (const k of ka) {
      if (!sameValue(ra[k], rb[k], depth - 1)) return false
    }
    return true
  }

  function sameRow(fresh: Record<string, unknown>, prev: Record<string, unknown>): boolean {
    // 键数前置：只遍历 fresh 键检测不到「字段从有值变为消失」（prev 独有键），
    // 后端一旦引入 exclude_none 类优化就会出现 429 徽标永久残留式 UI 停更。
    if (Object.keys(fresh).length !== Object.keys(prev).length) return false
    for (const k of Object.keys(fresh)) {
      if (fresh[k] === prev[k]) continue
      if (!NESTED_ROW_FIELDS.includes(k)) return false
      if (!sameValue(fresh[k], prev[k], 3)) return false
    }
    return true
  }

  const accountRows = computed<AccountWithStats[]>(() => {
    const nextCache = new Map<string, AccountWithStats>()
    const rows = accounts.value.map((a) => {
      const rot = rotationAccounts.value[a.id] || {}
      // 比较对象是「合并后的 row」与 prev：原先拿账号响应（12 键）与
      // 合并产物（约 29 键）比键数恒不等，缓存永不命中，8s 轮询每次
      // 全量重建几百账号 × 配额网格的整棵组件树。
      const row = { ...a, ...rot } as AccountWithStats
      const prev = rowCache.get(a.id)
      if (prev && sameRow(row as unknown as Record<string, unknown>, prev as unknown as Record<string, unknown>)) {
        nextCache.set(a.id, prev)
        return prev
      }
      nextCache.set(a.id, row)
      return row
    })
    rowCache.clear()
    for (const [k, v] of nextCache) rowCache.set(k, v)
    return rows
  })

  // 请求代次：轮询与操作后的 fetchAll 可能乱序返回，只让最新一次写状态。
  const fetchLatest = useLatest()
  // fetchAll 错误去重：同一错误文本只弹一次 toast，恢复后重新武装
  let lastFetchError = ''

  async function fetchAll() {
    const isLatest = fetchLatest.begin()
    loading.value = true
    try {
      const [accsRes, activeRes, rotationRes] = await Promise.allSettled([
        accountsApi.list(),
        accountsApi.getActive(),
        systemStore.fetchRotation(),
      ])
      if (!isLatest()) return

      if (accsRes.status === 'fulfilled') {
        accounts.value = accsRes.value
        // 恢复后清零去重门：下次故障重新提示
        lastFetchError = ''
      } else {
        const reasonMsg = accsRes.reason instanceof Error ? accsRes.reason.message : String(accsRes.reason)
        console.warn('获取账号列表失败，保留当前展示数据:', accsRes.reason)
        // 去重门（与 statsError 同款）：服务故障期轮询退避封顶 60s，
        // 每次都弹错会刷屏；同一错误只报一次，恢复后重新武装。
        if (reasonMsg !== lastFetchError) {
          lastFetchError = reasonMsg
          useToastStore().error(`获取账号列表失败: ${reasonMsg}`)
        }
      }

      if (activeRes.status === 'fulfilled') {
        activeAccount.value = activeRes.value
      } else {
        // 瞬时失败不代表没有激活账号：保留旧值，避免面板谎报「无激活账号」
        console.warn('获取激活账号失败，保留当前展示数据:', activeRes.reason)
      }
      return (
        accsRes.status === 'fulfilled' &&
        activeRes.status === 'fulfilled' &&
        rotationRes.status === 'fulfilled' &&
        rotationRes.value
      )
    } finally {
      if (isLatest()) loading.value = false
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

  /**
   * 账号操作的统一外壳（私有 helper，不导出给组件用）：成功/失败提示、
   * 失败返回值（统一 null）与刷新都在这里收口；需要返回 boolean 的
   * action 自己把 null 映射成 false，因此对外返回类型保持不变。
   */
  async function withAction<T>(
    fn: () => Promise<T>,
    opts: {
      failure: string
      /** 固定文案，或按结果动态生成（返回空串则不提示） */
      success?: string | ((result: T) => string | void)
      /** 成功后的刷新动作，默认只刷账号列表 */
      refresh?: () => Promise<unknown>
      /** activateAccount 失败时也要刷新，避免列表与激活态不一致 */
      refreshOnError?: boolean
    },
  ): Promise<T | null> {
    const toast = useToastStore()
    try {
      const result = await fn()
      if (typeof opts.success === 'function') {
        const message = opts.success(result)
        if (message) toast.success(message)
      } else if (opts.success) {
        toast.success(opts.success)
      }
      await (opts.refresh ?? fetchAll)()
      return result
    } catch (err: unknown) {
      toast.error(errorMessage(err, opts.failure))
      if (opts.refreshOnError) await (opts.refresh ?? fetchAll)()
      return null
    }
  }

  async function toggleAccountEnabled(id: string, enabled?: boolean) {
    return withAction(() => accountsApi.toggle(id, enabled), {
      success: (updated) => `账号已${updated.enabled ? '启用' : '停用'}`,
      failure: '切换状态失败',
    })
  }

  async function updateAccountGroup(id: string, group: string) {
    return withAction(() => accountsApi.updateGroup(id, group), {
      success: '分组已更新',
      failure: '更新分组失败',
      refresh: async () => {
        await fetchAll()
        await fetchGroups()
      },
    })
  }

  async function applyPreset(presetId: string) {
    applyingPresetId.value = presetId
    try {
      const res = await withAction(() => accountsApi.applyPreset(presetId), {
        success: (preset) => `已应用「${preset.preset_name}」: 启用 ${preset.enabled_count} 个`,
        failure: '应用方案失败',
      })
      return res !== null
    } finally {
      applyingPresetId.value = null
    }
  }

  async function createSnapshotPreset(name: string, description = '') {
    return withAction(() => accountsApi.createSnapshotPreset({ name, description }), {
      success: (created) => `方案「${created.name}」已保存`,
      failure: '保存方案失败',
      refresh: fetchPresets,
    })
  }

  async function deletePreset(presetId: string) {
    const res = await withAction(() => accountsApi.deletePreset(presetId), {
      success: '方案已删除',
      failure: '删除方案失败',
      refresh: fetchPresets,
    })
    return res !== null
  }

  async function activateAccount(id: string) {
    activatingId.value = id
    try {
      await withAction(() => accountsApi.activate(id), {
        success: (updated) => {
          // 激活成功后先就地更新激活账号，再交给 fetchAll 与后端对账
          activeAccount.value = updated
          return `已成功激活账号: ${updated.name || updated.email || updated.id}`
        },
        failure: '激活失败',
        refreshOnError: true,
      })
    } finally {
      activatingId.value = null
    }
  }

  async function deleteAccount(id: string) {
    await withAction(() => accountsApi.delete(id), { success: '账号已删除', failure: '删除失败' })
  }

  async function deleteCookieGroup(cookieId: string) {
    await withAction(() => accountsApi.deleteCookieGroup(cookieId), {
      success: (res) => `已成功删除该 Cookie 凭据下的全部 ${res.deleted} 个子账号`,
      failure: '删除失败',
    })
  }

  async function updateAccountName(id: string, name: string) {
    const res = await withAction(() => accountsApi.update(id, name), {
      success: '账号名称已更新',
      failure: '更新失败',
    })
    return res !== null
  }

  async function updateAccountProxy(id: string, proxyUrl: string | null) {
    const res = await withAction(() => accountsApi.updateProxy(id, proxyUrl), {
      success: proxyUrl ? `账号代理已更新: ${proxyUrl}` : '已恢复使用全局代理',
      failure: '代理更新失败',
    })
    return res !== null
  }

  async function updateAccountNotes(id: string, notes: string) {
    // 备注保存无成功提示（编辑框直接展示结果），与原实现一致
    const res = await withAction(() => accountsApi.updateNotes(id, notes), {
      failure: '备注更新失败',
    })
    return res !== null
  }

  async function importCookies(payload: ImportCookiesRequest) {
    importing.value = true
    try {
      const res = await withAction(() => accountsApi.importCookies(payload), {
        success: (result) => `导入成功: 解析出 ${result.cookie_count} 个 Cookie (u/${result.auth_user})`,
        failure: '导入失败',
      })
      return res !== null
    } finally {
      importing.value = false
    }
  }

  async function probeAndImport(payload: ProbeAndImportRequest) {
    importing.value = true
    try {
      const res = await withAction(() => accountsApi.probeAndImport(payload), {
        success: (result) => `已完成探测并导入 ${result.imported_count} 个账号`,
        failure: '自动探活导入失败',
      })
      return res !== null
    } finally {
      importing.value = false
    }
  }

  async function importBundle(payload: ImportBundleRequest) {
    importing.value = true
    try {
      const res = await withAction(() => accountsApi.importBundle(payload), {
        success: (result) => `批量导入成功: 已入库 ${result.imported_count} 个账号`,
        failure: '批量导入失败',
      })
      return res !== null
    } finally {
      importing.value = false
    }
  }

  async function detectAccountEmail(id: string) {
    return withAction(() => accountsApi.detectEmail(id), {
      // 未识别到邮箱时用 info 提示（不是 success），故在此自行分支
      success: (res) => {
        const tierSuffix = res.tier ? ` · 等级: ${res.tier}` : ''
        if (res.email) {
          useToastStore().success(`已成功识别账号邮箱: ${res.email}${tierSuffix}`)
        } else {
          useToastStore().info(`未在当前页面识别到有效邮箱${tierSuffix}`)
        }
      },
      failure: '识别邮箱失败',
    })
  }

  async function detectAccountTier(id: string) {
    return withAction(() => accountsApi.detectTier(id), {
      success: (res) => `已更新账号订阅等级: ${res.tier || 'Free'}`,
      failure: '探测订阅等级失败',
    })
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
    updateAccountNotes,
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
