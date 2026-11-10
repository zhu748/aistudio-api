<script setup lang="ts">
import { ref, computed, toRef, onMounted } from 'vue'
import type { AccountWithStats } from '@/types/accounts.ts'
import { useAccountsStore } from '@/stores/accounts.ts'
import { useSystemStore } from '@/stores/system.ts'
import { useToastStore } from '@/stores/toast.ts'
import { useConfirm } from '@/composables/useConfirm.ts'
import { systemApi } from '@/api/system.ts'
import { useCookieGroups, type CookieGroup } from './useCookieGroups.ts'
import CookieGroupBlock from './CookieGroupBlock.vue'
import AccountRow from './AccountRow.vue'
import Button from '@/components/ui/Button.vue'
import {
  Cookie,
  Search,
  Layers,
  Clock,
  Trash2,
  Plus,
  ChevronsUpDown,
} from 'lucide-vue-next'

const props = defineProps<{
  accounts: AccountWithStats[]
  activeId: string
}>()

const emit = defineEmits<{
  editName: [account: AccountWithStats]
  openImport: []
}>()

const accountsStore = useAccountsStore()
const systemStore = useSystemStore()
const toast = useToastStore()
const { confirm } = useConfirm()

// Search & status filter
const searchKeyword = ref('')
const statusFilter = ref<'all' | 'ready' | 'limited' | 'expired'>('all')

const statusCounts = computed(() => {
  let ready = 0
  let limited = 0
  let expired = 0
  for (const acc of props.accounts) {
    if (acc.session_expired) {
      expired += 1
    } else if (acc.is_available === false) {
      limited += 1
    } else {
      ready += 1
    }
  }
  return {
    all: props.accounts.length,
    ready,
    limited,
    expired,
  }
})

const filteredAccounts = computed<AccountWithStats[]>(() => {
  const kw = searchKeyword.value.trim().toLowerCase()
  return props.accounts.filter((acc) => {
    if (statusFilter.value === 'expired') {
      if (!acc.session_expired) return false
    } else if (statusFilter.value === 'limited') {
      if (acc.session_expired || acc.is_available !== false) return false
    } else if (statusFilter.value === 'ready') {
      if (acc.session_expired || acc.is_available === false) return false
    }

    if (!kw) return true
    const inName = (acc.name || '').toLowerCase().includes(kw)
    const inEmail = (acc.email || '').toLowerCase().includes(kw)
    const inId = acc.id.toLowerCase().includes(kw)
    const inAuthUser = `u/${acc.auth_user}`.toLowerCase().includes(kw) || acc.auth_user === kw
    const inTier = (acc.tier || 'free').toLowerCase().includes(kw)
    return inName || inEmail || inId || inAuthUser || inTier
  })
})

const { cookieGroups } = useCookieGroups(filteredAccounts, toRef(props, 'activeId'))

// Persist collapsed cookie group IDs
const STORAGE_COLLAPSED_KEY = 'aistudio_collapsed_cookie_ids'
const collapsedCookieIds = ref<Set<string>>(new Set())

onMounted(() => {
  try {
    const raw = localStorage.getItem(STORAGE_COLLAPSED_KEY)
    if (raw) {
      const parsed = JSON.parse(raw)
      if (Array.isArray(parsed)) {
        collapsedCookieIds.value = new Set(parsed)
      }
    }
  } catch {
    // ignore
  }
})

function saveCollapsedState(set: Set<string>) {
  try {
    localStorage.setItem(STORAGE_COLLAPSED_KEY, JSON.stringify(Array.from(set)))
  } catch {
    // ignore
  }
}

function isGroupCollapsed(cid: string): boolean {
  return collapsedCookieIds.value.has(cid)
}

function toggleCookieGroup(cid: string) {
  const next = new Set(collapsedCookieIds.value)
  if (next.has(cid)) {
    next.delete(cid)
  } else {
    next.add(cid)
  }
  collapsedCookieIds.value = next
  saveCollapsedState(next)
}

const allGroupsCollapsed = computed(() => {
  if (!cookieGroups.value.length) return false
  return cookieGroups.value.every((g) => collapsedCookieIds.value.has(g.id))
})

function toggleAllGroups() {
  if (allGroupsCollapsed.value) {
    collapsedCookieIds.value = new Set()
  } else {
    collapsedCookieIds.value = new Set(cookieGroups.value.map((g) => g.id))
  }
  saveCollapsedState(collapsedCookieIds.value)
}

// Global expand/collapse per-model quota grid
const forceExpandAllModels = ref(false)

function toggleAllModels() {
  forceExpandAllModels.value = !forceExpandAllModels.value
}

// PST midnight countdown
const hoursUntilPacificMidnight = computed(() => {
  const now = new Date()
  try {
    const parts = new Intl.DateTimeFormat('en-US', {
      timeZone: 'America/Los_Angeles',
      hour: 'numeric',
      minute: 'numeric',
      hour12: false,
    }).formatToParts(now)
    const h = Number(parts.find((p) => p.type === 'hour')?.value ?? 0) % 24
    const m = Number(parts.find((p) => p.type === 'minute')?.value ?? 0)
    return `${23 - h}h ${59 - m}m`
  } catch {
    const utcNow = now.getTime() + now.getTimezoneOffset() * 60000
    const pacificNow = new Date(utcNow - 8 * 3600000)
    return `${23 - pacificNow.getHours()}h ${59 - pacificNow.getMinutes()}m`
  }
})

const cleaningGhosts = ref(false)

async function handleCleanupGhosts() {
  cleaningGhosts.value = true
  try {
    const res = await systemApi.cleanupGhosts()
    if (res.cleaned_count > 0) {
      toast.success(`已清理 ${res.cleaned_count} 个已删除账号的状态残留`)
    } else {
      toast.info('未发现幽灵状态残留，凭据库与调度器完全一致')
    }
    await accountsStore.fetchAll()
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '清理失败'
    toast.error(msg)
  } finally {
    cleaningGhosts.value = false
  }
}

async function handleDeleteAccount(acc: AccountWithStats) {
  const name = acc.name || acc.email || acc.id
  const ok = await confirm({
    title: '删除子账号',
    message: `确定要删除子账号「${name}」吗？此操作不可撤销。`,
    confirmText: '确认删除',
    variant: 'danger',
  })
  if (ok) {
    await accountsStore.deleteAccount(acc.id)
  }
}

async function handleDeleteCookieGroup(group: CookieGroup) {
  const ok = await confirm({
    title: '删除整组 Cookie 凭据',
    message: `确定要删除凭据组「${group.name}」及其关联的全部 ${group.accounts.length} 个子账号吗？`,
    confirmText: '删除整组',
    variant: 'danger',
  })
  if (ok) {
    await accountsStore.deleteCookieGroup(group.id)
  }
}

async function handleReverifyAccount(acc: AccountWithStats) {
  await systemStore.clearCooldown({ account_id: acc.id })
  await accountsStore.activateAccount(acc.id)
}

async function handleClearAccountCooldown(accountId: string) {
  await systemStore.clearCooldown({ account_id: accountId })
  await accountsStore.fetchAll()
}

async function handleClearModelCooldown(accountId: string, model: string) {
  await systemStore.clearCooldown({ account_id: accountId, model })
  await accountsStore.fetchAll()
}
</script>

<template>
  <div class="bg-white border border-slate-200/90 rounded-xl shadow-2xs overflow-hidden">
    <!-- Consolidated Filter & View Toolbar -->
    <div class="px-4 py-3 border-b border-slate-100 bg-slate-50/50 flex flex-col lg:flex-row lg:items-center justify-between gap-3">
      <!-- Left: Search Input & Status Segmented Filter -->
      <div class="flex items-center gap-2.5 flex-wrap flex-1 min-w-0">
        <div class="relative w-full sm:w-60">
          <Search class="w-3.5 h-3.5 text-slate-400 absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" />
          <input
            v-model="searchKeyword"
            type="text"
            placeholder="搜索邮箱、备注、u/0、等级…"
            class="w-full pl-8 pr-3 py-1.5 bg-white border border-slate-200 rounded-lg text-xs outline-none focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 font-mono transition-all"
          >
        </div>

        <!-- Status Segmented Control with Live Counts -->
        <div class="flex items-center bg-slate-200/60 p-0.5 rounded-lg text-xs font-medium tabular-nums">
          <button
            type="button"
            class="px-2.5 py-1 rounded-md transition-all cursor-pointer"
            :class="statusFilter === 'all' ? 'bg-white text-slate-900 font-semibold shadow-2xs' : 'text-slate-600 hover:text-slate-900'"
            @click="statusFilter = 'all'"
          >
            全部 ({{ statusCounts.all }})
          </button>
          <button
            type="button"
            class="px-2.5 py-1 rounded-md transition-all cursor-pointer"
            :class="statusFilter === 'ready' ? 'bg-white text-emerald-700 font-semibold shadow-2xs' : 'text-slate-600 hover:text-slate-900'"
            @click="statusFilter = 'ready'"
          >
            就绪 ({{ statusCounts.ready }})
          </button>
          <button
            type="button"
            class="px-2.5 py-1 rounded-md transition-all cursor-pointer"
            :class="statusFilter === 'limited' ? 'bg-white text-amber-700 font-semibold shadow-2xs' : 'text-slate-600 hover:text-slate-900'"
            @click="statusFilter = 'limited'"
          >
            受限 ({{ statusCounts.limited }})
          </button>
          <button
            type="button"
            class="px-2.5 py-1 rounded-md transition-all cursor-pointer"
            :class="statusFilter === 'expired' ? 'bg-white text-rose-700 font-semibold shadow-2xs' : 'text-slate-600 hover:text-slate-900'"
            @click="statusFilter = 'expired'"
          >
            失效 ({{ statusCounts.expired }})
          </button>
        </div>
      </div>

      <!-- Right: Quota Reset Timer & View Toggles -->
      <div class="flex items-center gap-2 flex-wrap shrink-0">
        <div
          class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md bg-white border border-slate-200/80 text-[11px] text-slate-500 font-mono tabular-nums"
          title="美西时间 00:00 (PST/PDT) 自动重置每日模型配额计数"
        >
          <Clock class="w-3 h-3 text-brand-600" />
          <span>配额重置: {{ hoursUntilPacificMidnight }}</span>
        </div>

        <button
          type="button"
          class="px-2.5 py-1 text-xs font-medium rounded-lg border transition-colors cursor-pointer flex items-center gap-1"
          :class="
            forceExpandAllModels
              ? 'bg-brand-50 text-brand-700 border-brand-200'
              : 'bg-white text-slate-600 border-slate-200/80 hover:bg-slate-100'
          "
          title="一键展开或收起所有账号的模型今日配额面板"
          @click="toggleAllModels"
        >
          <Layers class="w-3.5 h-3.5" />
          <span>{{ forceExpandAllModels ? '收起配额' : '展开配额' }}</span>
        </button>

        <button
          type="button"
          class="px-2.5 py-1 text-xs font-medium text-slate-600 bg-white border border-slate-200/80 hover:bg-slate-100 rounded-lg transition-colors cursor-pointer flex items-center gap-1"
          @click="toggleAllGroups"
        >
          <ChevronsUpDown class="w-3.5 h-3.5 text-slate-400" />
          <span>{{ allGroupsCollapsed ? '展开分组' : '折叠分组' }}</span>
        </button>

        <button
          type="button"
          class="px-2 py-1 text-xs font-medium text-slate-500 hover:text-rose-700 hover:bg-rose-50 rounded-lg transition-colors cursor-pointer flex items-center gap-1"
          title="清除已从存储中删除但残留在调度器中的幽灵状态"
          :disabled="cleaningGhosts"
          @click="handleCleanupGhosts"
        >
          <Trash2 class="w-3.5 h-3.5" />
          <span class="hidden sm:inline">{{ cleaningGhosts ? '清理中…' : '清理残留' }}</span>
        </button>
      </div>
    </div>

    <!-- Tree Body -->
    <div class="divide-y divide-slate-100">
      <CookieGroupBlock
        v-for="group in cookieGroups"
        :key="group.id"
        :group="group"
        :collapsed="isGroupCollapsed(group.id)"
        @toggle="toggleCookieGroup(group.id)"
        @delete-group="handleDeleteCookieGroup(group)"
      >
        <AccountRow
          v-for="acc in group.accounts"
          :key="acc.id"
          :account="acc"
          :active="acc.id === activeId"
          :activating="accountsStore.activatingId === acc.id"
          :force-expanded="forceExpandAllModels"
          @activate="accountsStore.activateAccount(acc.id)"
          @delete="handleDeleteAccount(acc)"
          @edit-name="emit('editName', acc)"
          @clear-cooldown="acc.session_expired ? handleReverifyAccount(acc) : handleClearAccountCooldown(acc.id)"
          @clear-model="handleClearModelCooldown(acc.id, $event)"
        />
      </CookieGroupBlock>

      <!-- Empty State -->
      <div
        v-if="!filteredAccounts.length"
        class="py-14 text-center space-y-3"
      >
        <div class="w-10 h-10 rounded-xl bg-slate-100 text-slate-400 flex items-center justify-center mx-auto">
          <Cookie class="w-5 h-5" />
        </div>
        <div class="space-y-1">
          <div class="text-sm font-semibold text-slate-700">
            {{ searchKeyword || statusFilter !== 'all' ? '未找到匹配的账号节点' : '凭据池为空' }}
          </div>
          <p class="text-xs text-slate-400 max-w-sm mx-auto">
            {{ searchKeyword || statusFilter !== 'all' ? '请尝试清空搜索词或切换上方状态筛选条件。' : '导入 Google AI Studio Cookie 凭据即可开始多账号粘性轮询。' }}
          </p>
        </div>
        <div v-if="!searchKeyword && statusFilter === 'all'">
          <Button
            variant="primary"
            size="sm"
            @click="emit('openImport')"
          >
            <Plus class="w-3.5 h-3.5" />
            <span>立即导入凭据</span>
          </Button>
        </div>
      </div>
    </div>
  </div>
</template>
