<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import type { AccountWithStats } from '@/types/accounts.ts'
import Badge from '@/components/ui/Badge.vue'
import Button from '@/components/ui/Button.vue'
import ModelQuotaGrid from './ModelQuotaGrid.vue'
import {
  CheckCircle2,
  Clock,
  Trash2,
  Edit2,
  Layers,
  RotateCcw,
  AlertCircle,
  RefreshCw,
  Mail,
  Tag,
  Check,
  Globe,
} from 'lucide-vue-next'
import { useAccountsStore } from '@/stores/accounts.ts'

const props = defineProps<{
  account: AccountWithStats
  active: boolean
  activating?: boolean
  forceExpanded?: boolean
  selectable?: boolean
  selected?: boolean
}>()

const emit = defineEmits<{
  activate: []
  delete: []
  editName: []
  clearCooldown: []
  clearModel: [model: string]
  select: []
  details: []
}>()

const modelsExpanded = ref(false)

watch(
  () => props.forceExpanded,
  (val) => {
    if (val !== undefined) {
      modelsExpanded.value = val
    }
  },
  { immediate: true },
)

const todayRequests = computed(() => {
  const reqs = props.account.model_requests || {}
  return Object.values(reqs).reduce((sum, count) => sum + (Number(count) || 0), 0)
})

const modelCount = computed(() => {
  return Object.keys(props.account.model_requests || {}).length
})

const accountsStore = useAccountsStore()
const detectingEmail = ref(false)
const detectingTier = ref(false)
const togglingEnabled = ref(false)
const editingGroup = ref(false)
const groupInput = ref('')

const accountTier = computed(() => {
  return props.account.tier || 'Free'
})

const proxyShort = computed(() => {
  const p = props.account.proxy_url
  if (!p) return ''
  try {
    const u = new URL(p)
    return u.host
  } catch {
    return p.length > 22 ? `${p.slice(0, 22)}…` : p
  }
})

const isEnabled = computed(() => {
  return props.account.enabled !== false
})

async function handleToggleEnabled() {
  togglingEnabled.value = true
  try {
    await accountsStore.toggleAccountEnabled(props.account.id)
  } finally {
    togglingEnabled.value = false
  }
}

function startEditGroup() {
  groupInput.value = props.account.group || 'default'
  editingGroup.value = true
}

async function saveGroup() {
  if (!editingGroup.value) return
  const target = groupInput.value.trim() || 'default'
  editingGroup.value = false
  if (target !== (props.account.group || 'default')) {
    await accountsStore.updateAccountGroup(props.account.id, target)
  }
}

async function handleDetectEmail() {
  detectingEmail.value = true
  try {
    await accountsStore.detectAccountEmail(props.account.id)
  } finally {
    detectingEmail.value = false
  }
}

async function handleDetectTier() {
  detectingTier.value = true
  try {
    await accountsStore.detectAccountTier(props.account.id)
  } finally {
    detectingTier.value = false
  }
}
</script>

<template>
  <div
    class="relative bg-white border rounded-xl px-3.5 py-2.5 transition-all"
    :class="[
      !isEnabled
        ? 'border-slate-200/80 bg-slate-50/70 opacity-65'
        : account.session_expired
          ? 'border-rose-300 bg-rose-50/20'
          : active
            ? 'border-brand-400 bg-white ring-1 ring-brand-500/15 shadow-2xs'
            : 'border-slate-200/90 hover:border-slate-300',
    ]"
  >
    <!-- Sub-Account Blade Row -->
    <div class="flex flex-col lg:flex-row lg:items-center justify-between gap-2.5">
      <!-- Left Zone: Switch + Index + Identity + Badges -->
      <div class="flex items-center gap-2.5 min-w-0 flex-wrap sm:flex-nowrap">
        <input
          v-if="selectable"
          type="checkbox"
          :checked="selected"
          :aria-label="`选择账号 ${account.email || account.name || account.id}`"
          class="accent-brand-600 w-4 h-4"
          @change="emit('select')"
        >
        <!-- Enable/Disable Switch -->
        <button
          type="button"
          role="switch"
          :aria-checked="isEnabled"
          :disabled="togglingEnabled"
          class="relative inline-flex h-4.5 w-8 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none"
          :class="[
            isEnabled ? 'bg-brand-600' : 'bg-slate-300',
            { 'opacity-50 cursor-wait': togglingEnabled },
          ]"
          title="启用或禁用此账号参与网关调度"
          @click="handleToggleEnabled"
        >
          <span
            class="pointer-events-none inline-block h-3.5 w-3.5 transform rounded-full bg-white shadow-2xs ring-0 transition duration-200 ease-in-out"
            :class="isEnabled ? 'translate-x-3.5' : 'translate-x-0'"
          />
        </button>

        <!-- User Index Badge: u/0, u/1... -->
        <span
          class="px-2 py-0.5 rounded-md text-[11px] font-mono font-bold shrink-0 tabular-nums"
          :class="[
            !isEnabled
              ? 'bg-slate-200 text-slate-500'
              : account.session_expired
                ? 'bg-rose-100 text-rose-800 border border-rose-200'
                : active
                  ? 'bg-slate-900 text-brand-400'
                  : 'bg-slate-100 text-slate-700 border border-slate-200/80',
          ]"
        >
          u/{{ account.auth_user }}
        </span>

        <!-- Primary Identity & Secondary Meta -->
        <div class="min-w-0 flex-1">
          <div class="flex items-center gap-1.5 flex-wrap">
            <button
              class="font-semibold text-slate-900 hover:text-brand-700 text-sm truncate max-w-[260px] cursor-pointer text-left"
              :title="`${account.email || account.name || 'Google Account'} · 查看账号详情与最近调用`"
              @click="emit('details')"
            >
              {{ account.email || account.name || 'Google Account' }}
            </button>

            <button
              type="button"
              class="text-slate-300 hover:text-slate-600 transition-colors cursor-pointer p-0.5"
              title="编辑备注名称与独立代理"
              @click="emit('editName')"
            >
              <Edit2 class="w-3 h-3" />
            </button>

            <!-- Benefit Tier Badge: Free / Plus / Pro / Ultra -->
            <button
              type="button"
              class="px-1.5 py-0.5 rounded text-[10px] font-mono font-semibold shrink-0 transition-opacity hover:opacity-80 cursor-pointer border"
              :class="[
                accountTier === 'Ultra' || accountTier === 'Ultra_20X'
                  ? 'bg-sky-50 text-sky-700 border-sky-200'
                  : accountTier === 'Pro'
                    ? 'bg-amber-50 text-amber-700 border-amber-200'
                    : accountTier === 'Plus'
                      ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                      : 'bg-slate-100 text-slate-600 border-slate-200/80',
                { 'opacity-50 cursor-wait': detectingTier },
              ]"
              :disabled="detectingTier"
              title="点击重新探测此子账号的订阅等级 (Free / Plus / Pro / Ultra)"
              @click="handleDetectTier"
            >
              {{ detectingTier ? '探测中…' : accountTier }}
            </button>

            <!-- Group Tag / Inline Editor -->
            <div class="inline-flex items-center">
              <div
                v-if="editingGroup"
                class="flex items-center gap-1"
              >
                <input
                  v-model="groupInput"
                  type="text"
                  class="px-1.5 py-0.5 text-[11px] font-mono border border-brand-400 rounded focus:outline-none w-20"
                  placeholder="group"
                  @keydown.enter="saveGroup"
                  @blur="saveGroup"
                >
                <button
                  type="button"
                  class="text-emerald-600 hover:text-emerald-700 cursor-pointer"
                  @click="saveGroup"
                >
                  <Check class="w-3 h-3" />
                </button>
              </div>
              <button
                v-else
                type="button"
                class="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-mono font-medium text-slate-500 bg-slate-50 hover:bg-slate-100 transition-colors cursor-pointer border border-slate-200/80"
                title="点击修改所属分组"
                @click="startEditGroup"
              >
                <Tag class="w-2.5 h-2.5 text-slate-400" />
                <span>{{ account.group || 'default' }}</span>
              </button>
            </div>

            <!-- Per-Account Proxy Badge -->
            <span
              v-if="account.proxy_url"
              class="inline-flex items-center gap-0.5 px-1.5 py-0.5 rounded text-[10px] font-mono font-medium text-sky-700 bg-sky-50 border border-sky-200/70 shrink-0"
              :title="`独立代理: ${account.proxy_url}`"
            >
              <Globe class="w-2.5 h-2.5 text-sky-500" />
              <span>{{ proxyShort }}</span>
            </span>
          </div>

          <div class="text-[11px] text-slate-400 font-mono flex items-center flex-wrap gap-x-1.5 gap-y-0.5 mt-0.5">
            <span
              v-if="account.email && account.name && account.name !== account.email"
              class="text-slate-500 truncate max-w-[180px]"
            >{{ account.name }} ·</span>
            <span
              v-else-if="!account.email"
              class="text-amber-700 bg-amber-50 px-1 py-px rounded text-[10px] border border-amber-200/60 shrink-0"
            >未记录邮箱</span>
            <span
              class="truncate max-w-full"
              :title="account.id"
            >ID: {{ account.id }}</span>
          </div>
        </div>
      </div>

      <!-- Right Zone: Telemetry Readout + Status Pill + Action Cluster -->
      <div class="flex items-center justify-between lg:justify-end gap-2.5 shrink-0 flex-wrap">
        <!-- Tabular Telemetry -->
        <div
          class="flex items-center gap-2 text-xs font-mono tabular-nums"
          title="账号调用数含调度重试事件，最终客户端调用请查看请求记录"
        >
          <span class="text-slate-600">今日 <strong class="text-slate-900">{{ todayRequests }}</strong></span>
          <span class="text-slate-300">·</span>
          <span class="text-slate-500">累计 <strong class="text-slate-700">{{ account.requests || 0 }}</strong></span>
          <Badge
            variant="green"
            size="sm"
          >
            {{ account.success || 0 }}
          </Badge>
          <Badge
            :variant="(account.rate_limited || 0) > 0 ? 'amber' : 'gray'"
            size="sm"
          >
            429: {{ account.rate_limited || 0 }}
          </Badge>
        </div>

        <!-- Status Pill -->
        <div
          v-if="!isEnabled"
          class="inline-flex items-center gap-1 text-[11px] font-medium text-slate-500 bg-slate-100 px-2 py-0.5 rounded-md border border-slate-200"
        >
          <span>已停用</span>
        </div>
        <div
          v-else-if="account.session_expired"
          class="inline-flex items-center gap-1 text-[11px] font-semibold text-rose-700 bg-rose-50 px-2 py-0.5 rounded-md border border-rose-200"
          title="该账号登录态已失效，更新凭据后可点击恢复"
        >
          <AlertCircle class="w-3 h-3 text-rose-600" />
          <span>凭据失效</span>
        </div>
        <div
          v-else-if="active && account.availability !== 'partial'"
          class="inline-flex items-center gap-1 text-[11px] font-semibold text-emerald-700 bg-emerald-50 px-2 py-0.5 rounded-md border border-emerald-200"
        >
          <CheckCircle2 class="w-3 h-3" />
          <span>当前激活</span>
        </div>
        <div
          v-else-if="account.availability === 'partial' || account.is_available === false"
          class="inline-flex items-center gap-1 text-[11px] font-semibold text-amber-700 bg-amber-50 px-2 py-0.5 rounded-md border border-amber-200"
        >
          <Clock class="w-3 h-3" />
          <span>{{ active ? '当前激活 · 模型冷却' : '部分模型冷却' }}</span>
        </div>
        <div
          v-else
          class="px-2 py-0.5 text-[11px] text-slate-500 bg-slate-50 border border-slate-200/70 rounded-md font-medium"
        >
          就绪
        </div>

        <!-- Compact Action Cluster -->
        <div class="flex items-center gap-1 lg:border-l lg:border-slate-100 lg:pl-2">
          <button
            v-if="account.session_expired"
            type="button"
            class="px-2 py-1 text-[11px] font-medium text-rose-700 bg-rose-50 hover:bg-rose-100 border border-rose-200 rounded-md transition-colors cursor-pointer flex items-center gap-1"
            title="解除失效状态并重新探测激活"
            @click="emit('clearCooldown')"
          >
            <RefreshCw class="w-3 h-3" />
            <span>恢复</span>
          </button>

          <Button
            v-if="!active && !account.session_expired && isEnabled"
            variant="secondary"
            size="xs"
            :loading="activating"
            @click="emit('activate')"
          >
            <span>激活</span>
          </Button>

          <button
            type="button"
            class="px-1.5 py-1 text-[11px] font-mono rounded-md transition-colors cursor-pointer flex items-center gap-1 border"
            :class="
              modelsExpanded
                ? 'text-brand-700 bg-brand-50 border-brand-200'
                : 'text-slate-500 bg-white border-slate-200/80 hover:bg-slate-50 hover:text-slate-800'
            "
            title="展开/收起各模型今日配额明细"
            @click="modelsExpanded = !modelsExpanded"
          >
            <Layers class="w-3 h-3" />
            <span v-if="modelCount > 0">{{ modelCount }}</span>
          </button>

          <button
            type="button"
            class="p-1 text-slate-400 hover:text-brand-600 hover:bg-slate-100 rounded-md transition-colors cursor-pointer"
            :class="{ 'opacity-50 cursor-not-allowed': detectingEmail }"
            title="探测真实邮箱与订阅等级"
            :disabled="detectingEmail"
            @click="handleDetectEmail"
          >
            <Mail class="w-3.5 h-3.5" />
          </button>

          <button
            v-if="(account.rate_limited || 0) > 0 && !account.session_expired"
            type="button"
            class="p-1 text-amber-600 hover:text-amber-700 hover:bg-amber-50 rounded-md transition-colors cursor-pointer"
            title="清除此账号 429 锁定"
            @click="emit('clearCooldown')"
          >
            <RotateCcw class="w-3.5 h-3.5" />
          </button>

          <button
            type="button"
            class="p-1 text-slate-300 hover:text-rose-600 hover:bg-rose-50 rounded-md transition-colors cursor-pointer"
            title="删除此子账号"
            @click="emit('delete')"
          >
            <Trash2 class="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    </div>

    <!-- Level 3: Per-Model Independent Quota/Rate Limits -->
    <ModelQuotaGrid
      v-if="modelsExpanded"
      :account="account"
      @clear-model="emit('clearModel', $event)"
    />
  </div>
</template>
