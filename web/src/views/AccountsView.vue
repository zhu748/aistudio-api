<script setup lang="ts">
import { ref, computed } from 'vue'
import { useAccountsStore } from '@/stores/accounts.ts'
import { useSystemStore } from '@/stores/system.ts'
import { usePolling } from '@/composables/usePolling.ts'
import { usePageRefresh } from '@/composables/usePageRefresh.ts'
import { useConfirm } from '@/composables/useConfirm.ts'
import type { AccountWithStats } from '@/types/accounts.ts'
import Button from '@/components/ui/Button.vue'
import Modal from '@/components/ui/Modal.vue'
import AccountTable from '@/components/accounts/AccountTable.vue'
import CookieImportModal from '@/components/modals/CookieImportModal.vue'
import EditAccountModal from '@/components/modals/EditAccountModal.vue'
import {
  Plus,
  RotateCcw,
  ArrowRightLeft,
  BookmarkPlus,
  SlidersHorizontal,
  Trash2,
  Folder,
  Check,
} from 'lucide-vue-next'

const accountsStore = useAccountsStore()
const systemStore = useSystemStore()
const { confirm } = useConfirm()

const importModalOpen = ref(false)
const editModalOpen = ref(false)
const editingAccount = ref<AccountWithStats | null>(null)
const snapshotModalOpen = ref(false)
const snapshotName = ref('')
const snapshotDesc = ref('')
const selectedGroup = ref<string>('ALL')

// 轮询降频拆分（P8）：账号列表/活跃账号/调度统计是离频变化数据，8s 轮询；
// 预设方案与分组只在用户操作后变化（各 mutation 已自行刷新），降到 60s
// 即可，把周期请求数从 5 个/8s 降到 3 个/8s + 2 个/60s。
usePolling(accountsStore.fetchAll, 8000)
usePageRefresh(accountsStore.fetchAll)
usePolling(async () => {
  await Promise.all([accountsStore.fetchPresets(), accountsStore.fetchGroups()])
}, 60000)

const allGroups = computed(() => {
  const set = new Set<string>()
  accountsStore.accounts.forEach((a) => {
    if (a.group) set.add(a.group)
  })
  accountsStore.groups.forEach((g) => {
    if (g) set.add(g)
  })
  return Array.from(set).sort()
})

const filteredAccounts = computed(() => {
  if (selectedGroup.value === 'ALL') {
    return accountsStore.accountRows
  }
  return accountsStore.accountRows.filter((a) => (a.group || 'default') === selectedGroup.value)
})

const enabledCount = computed(() => {
  return accountsStore.accounts.filter((a) => a.enabled !== false).length
})

function handleEditName(acc: AccountWithStats) {
  editingAccount.value = acc
  editModalOpen.value = true
}

async function handleClearAllCooldowns() {
  const ok = await confirm({
    title: '重置全部 429 冷却锁定',
    message: '确定要清除所有账号下的全部模型 429 冷却与配额耗尽标记吗？',
    confirmText: '确认重置',
    variant: 'warning',
  })
  if (ok) {
    await systemStore.clearCooldown()
    await accountsStore.fetchAll()
  }
}

async function handleForceNext() {
  await systemStore.forceNextAccount()
  await accountsStore.fetchAll()
}

async function handleApplyPreset(presetId: string) {
  await accountsStore.applyPreset(presetId)
}

async function handleSaveSnapshot() {
  if (!snapshotName.value.trim()) return
  await accountsStore.createSnapshotPreset(snapshotName.value.trim(), snapshotDesc.value.trim())
  snapshotName.value = ''
  snapshotDesc.value = ''
  snapshotModalOpen.value = false
}

async function handleDeletePreset(presetId: string, name: string) {
  const ok = await confirm({
    title: '删除预设方案',
    message: `确定删除自定义账号方案「${name}」吗？`,
    confirmText: '确认删除',
    variant: 'danger',
  })
  if (ok) {
    await accountsStore.deletePreset(presetId)
  }
}
</script>

<template>
  <div class="space-y-4">
    <!-- Consolidated Top Control Deck: Groups + Primary Operations + Presets -->
    <div class="bg-white border border-slate-200/90 rounded-xl p-4 shadow-2xs space-y-3">
      <!-- Row 1: Group Filter Tabs & Primary Action Buttons -->
      <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
        <!-- Group Tabs -->
        <div class="flex items-center gap-1.5 flex-wrap">
          <button
            type="button"
            class="px-2.5 py-1.5 rounded-lg text-xs font-medium transition-all cursor-pointer tabular-nums"
            :class="[
              selectedGroup === 'ALL'
                ? 'bg-slate-900 text-white shadow-2xs'
                : 'text-slate-600 hover:bg-slate-100 bg-slate-50 border border-slate-200/70',
            ]"
            @click="selectedGroup = 'ALL'"
          >
            全部分组 ({{ accountsStore.accounts.length }})
          </button>

          <button
            v-for="grp in allGroups"
            :key="grp"
            type="button"
            class="px-2.5 py-1.5 rounded-lg text-xs font-medium transition-all cursor-pointer flex items-center gap-1.5"
            :class="[
              selectedGroup === grp
                ? 'bg-brand-600 text-white shadow-2xs'
                : 'text-slate-600 hover:bg-slate-100 bg-slate-50 border border-slate-200/70',
            ]"
            @click="selectedGroup = grp"
          >
            <Folder class="w-3 h-3 opacity-75" />
            <span>{{ grp }}</span>
          </button>
        </div>

        <!-- Primary Action Cluster -->
        <div class="flex items-center gap-2 flex-wrap shrink-0">
          <Button
            variant="outline"
            size="sm"
            :loading="systemStore.resettingCooldown"
            title="清除所有账号的 429 冷却与配额耗尽标记"
            @click="handleClearAllCooldowns"
          >
            <RotateCcw class="w-3.5 h-3.5" />
            <span>重置锁定</span>
          </Button>

          <Button
            variant="outline"
            size="sm"
            :loading="systemStore.switchingNext"
            title="手动顺延切换至下一个可用账号"
            @click="handleForceNext"
          >
            <ArrowRightLeft class="w-3.5 h-3.5" />
            <span>切换下一账号</span>
          </Button>

          <Button
            variant="primary"
            size="sm"
            @click="importModalOpen = true"
          >
            <Plus class="w-3.5 h-3.5" />
            <span>导入凭据</span>
          </Button>
        </div>
      </div>

      <!-- Row 2: Compact Presets Strip -->
      <div class="pt-2.5 border-t border-slate-100 flex items-center justify-between gap-2 flex-wrap">
        <div class="flex items-center gap-1.5 flex-wrap">
          <span class="inline-flex items-center gap-1 text-[11px] font-semibold text-slate-500 mr-1">
            <SlidersHorizontal class="w-3 h-3 text-brand-600" />
            <span>预设池方案:</span>
          </span>

          <div
            v-for="p in accountsStore.presets"
            :key="p.id"
            class="inline-flex items-center rounded-md border text-[11px] font-medium transition-all"
            :class="[
              accountsStore.applyingPresetId === p.id
                ? 'bg-brand-50 border-brand-300 text-brand-800'
                : 'bg-slate-50/80 border-slate-200/80 text-slate-700 hover:bg-slate-100 hover:border-slate-300',
            ]"
          >
            <button
              type="button"
              class="px-2 py-0.5 cursor-pointer flex items-center gap-1"
              :title="p.description || p.name"
              :disabled="accountsStore.applyingPresetId !== null"
              @click="handleApplyPreset(p.id)"
            >
              <span>{{ p.name }}</span>
            </button>
            <button
              v-if="!p.is_system"
              type="button"
              class="pr-1.5 pl-0.5 py-0.5 text-slate-400 hover:text-rose-600 transition-colors cursor-pointer"
              title="删除此方案"
              @click.stop="handleDeletePreset(p.id, p.name)"
            >
              <Trash2 class="w-3 h-3" />
            </button>
          </div>
        </div>

        <button
          type="button"
          class="inline-flex items-center gap-1 text-[11px] font-medium text-brand-700 hover:text-brand-800 bg-brand-50 hover:bg-brand-100/70 border border-brand-200/60 px-2 py-0.5 rounded-md transition-colors cursor-pointer shrink-0"
          @click="snapshotModalOpen = true"
        >
          <BookmarkPlus class="w-3 h-3" />
          <span>保存当前方案</span>
        </button>
      </div>
    </div>

    <!-- Account Table -->
    <AccountTable
      :accounts="filteredAccounts"
      :active-id="accountsStore.activeId"
      @edit-name="handleEditName"
      @open-import="importModalOpen = true"
    />

    <!-- Snapshot Save Modal (Unified Modal Component) -->
    <Modal
      v-model="snapshotModalOpen"
      max-width="sm"
      title="保存账号启用快照"
      subtitle="将当前已启用的账号组合保存为快捷切换预设"
    >
      <div class="space-y-3.5 text-xs">
        <div class="p-2.5 bg-brand-50/60 border border-brand-200/70 rounded-lg text-slate-700 tabular-nums">
          当前快照将记录 <strong class="text-brand-700 font-mono">{{ enabledCount }}</strong> 个已启用账号
        </div>
        <div class="space-y-1.5">
          <label class="block font-semibold text-slate-700">方案名称</label>
          <input
            v-model="snapshotName"
            type="text"
            placeholder="如：主力高并发池、夜间备份池"
            class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
          >
        </div>
        <div class="space-y-1.5">
          <label class="block font-semibold text-slate-700">方案备注 <span class="text-slate-400 font-normal">(可选)</span></label>
          <input
            v-model="snapshotDesc"
            type="text"
            placeholder="说明此预设方案的适用场景"
            class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
          >
        </div>
      </div>

      <template #footer>
        <Button
          variant="ghost"
          size="sm"
          @click="snapshotModalOpen = false"
        >
          取消
        </Button>
        <Button
          variant="primary"
          size="sm"
          :disabled="!snapshotName.trim()"
          @click="handleSaveSnapshot"
        >
          <Check class="w-3.5 h-3.5" />
          <span>确认保存</span>
        </Button>
      </template>
    </Modal>

    <!-- Cookie Import Modal -->
    <CookieImportModal v-model="importModalOpen" />

    <!-- Edit Account Name Modal -->
    <EditAccountModal
      v-model="editModalOpen"
      :account="editingAccount"
    />
  </div>
</template>
