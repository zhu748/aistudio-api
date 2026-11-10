<script setup lang="ts">
import type { CookieGroup } from './useCookieGroups.ts'
import { formatDate } from '@/utils/format.ts'
import {
  Cookie,
  ChevronDown,
  ChevronRight,
  FolderMinus,
} from 'lucide-vue-next'

defineProps<{
  group: CookieGroup
  collapsed: boolean
}>()

const emit = defineEmits<{
  toggle: []
  deleteGroup: []
}>()
</script>

<template>
  <div class="transition-colors">
    <!-- Level 1: Cookie Credential Parent Node -->
    <div
      class="px-4 py-3 flex items-center justify-between gap-3 cursor-pointer select-none transition-colors"
      :class="[
        group.hasActive ? 'bg-brand-50/25 hover:bg-brand-50/40' : 'bg-white hover:bg-slate-50/80',
      ]"
      @click="emit('toggle')"
    >
      <div class="flex items-center gap-2.5 min-w-0">
        <!-- Expand Chevron -->
        <button
          type="button"
          class="p-1 rounded-md text-slate-400 hover:text-slate-700 hover:bg-slate-200/60 transition-colors"
        >
          <ChevronDown
            v-if="!collapsed"
            class="w-4 h-4"
          />
          <ChevronRight
            v-else
            class="w-4 h-4"
          />
        </button>

        <!-- Cookie Icon -->
        <div
          class="w-7 h-7 rounded-lg flex items-center justify-center shrink-0 border"
          :class="
            group.hasActive
              ? 'bg-slate-900 text-brand-400 border-slate-800'
              : 'bg-slate-100 text-slate-600 border-slate-200/80'
          "
        >
          <Cookie class="w-3.5 h-3.5" />
        </div>

        <!-- Group Info -->
        <div class="truncate">
          <div class="flex items-center gap-2">
            <span class="font-semibold text-slate-900 text-xs tracking-tight">
              {{ group.name }}
            </span>
            <span class="text-[11px] font-mono text-slate-400 tabular-nums">
              · {{ group.accounts.length }} 个子账号
            </span>
            <span
              v-if="group.hasActive"
              class="px-1.5 py-0.5 text-[10px] font-semibold rounded bg-emerald-50 text-emerald-700 border border-emerald-200/70"
            >
              含激活节点
            </span>
          </div>
          <div class="text-[11px] text-slate-400 font-mono flex items-center gap-2 mt-0.5 tabular-nums">
            <span>导入: {{ formatDate(group.createdAt) }}</span>
            <span>·</span>
            <span
              class="truncate max-w-[160px]"
              :title="group.id"
            >指纹: {{ String(group.id || '').replace(/^cookie_/, '') }}</span>
          </div>
        </div>
      </div>

      <!-- Group Summary & Group Actions -->
      <div
        class="flex items-center gap-3 shrink-0"
        @click.stop
      >
        <div class="hidden sm:flex items-center gap-2.5 text-xs font-mono tabular-nums">
          <span class="text-slate-500">累计调用 <strong class="text-slate-800">{{ group.totalRequests }}</strong></span>
          <span class="text-slate-200">|</span>
          <span :class="group.totalRateLimited > 0 ? 'text-amber-600 font-semibold' : 'text-slate-400'">
            429: {{ group.totalRateLimited }}
          </span>
        </div>

        <button
          type="button"
          class="flex items-center gap-1 px-2 py-1 text-[11px] text-slate-400 hover:text-rose-600 hover:bg-rose-50 rounded-md transition-colors cursor-pointer"
          title="删除整份 Cookie 及其下所有子账号"
          @click="emit('deleteGroup')"
        >
          <FolderMinus class="w-3.5 h-3.5" />
          <span class="hidden md:inline">删除整组</span>
        </button>
      </div>
    </div>

    <!-- Level 2: Sub-Accounts Container -->
    <div
      v-if="!collapsed"
      class="bg-slate-50/60 px-4 sm:px-5 py-2.5 border-t border-slate-100"
    >
      <div class="space-y-2">
        <slot />
      </div>
    </div>
  </div>
</template>
