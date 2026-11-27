<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRouter } from 'vue-router'
import type { ThoughtSignatureItem, ThoughtSignatureTrace } from '@/api/operations.ts'
import {
  sigActionLabels,
  sigCarrierLabels,
  sigKindLabels,
  sigProtocolLabels,
  sigReasonLabels,
  sigSourceLabels,
} from '@/utils/requests.ts'
import SegmentedControl from '@/components/ui/SegmentedControl.vue'

const props = defineProps<{
  trace: ThoughtSignatureTrace | null | undefined
  requestId: string
}>()

const router = useRouter()

const activeList = ref<'client_in' | 'upstream_out' | 'upstream_in' | 'client_out'>('upstream_out')

const bareLeadTurns = computed(() => {
  const t = props.trace
  if (!t) return 0
  // 兼容旧记录：没有 lead 口径时退回 fc_unsigned。
  return t.upstream_out.fc_unsigned_lead_turns ?? t.upstream_out.fc_unsigned
})

const hasRisk = computed(() => {
  const t = props.trace
  if (!t) return false
  return bareLeadTurns.value > 0 || t.replay.conflicts > 0 || t.client_in.dropped > 0
})

const hasFallback = computed(() => {
  const t = props.trace
  if (!t) return false
  return t.replay.skip_fills > 0 || t.client_out.dropped > 0
})

const bannerClass = computed(() => {
  if (hasRisk.value) return 'bg-rose-50 border-rose-200 text-rose-900'
  if (hasFallback.value) return 'bg-amber-50 border-amber-200 text-amber-900'
  return 'bg-emerald-50 border-emerald-200 text-emerald-900'
})

const bannerTitle = computed(() => {
  const t = props.trace
  if (!t) return ''
  if (bareLeadTurns.value > 0) {
    return `存在 ${bareLeadTurns.value} 轮首个函数调用未带签名直接发往上游（存在 400 风险）`
  }
  if (t.replay.conflicts > 0) {
    return `检测到 ${t.replay.conflicts} 轮历史签名冲突或越权，已按安全策略扣留真签名`
  }
  if (t.client_in.dropped > 0) {
    return `客户端回传中有 ${t.client_in.dropped} 条非法或跨协议签名已过滤丢弃`
  }
  if (t.replay.skip_fills > 0) {
    return `已通过 SKIP_THOUGHT_SIGNATURE 兜底补填 ${t.replay.skip_fills} 个缺失签名的函数调用`
  }
  if (t.client_out.dropped > 0) {
    return `上游返回 ${t.upstream_in.total_sigs} 条签名，其中 ${t.client_out.dropped} 条因兼容协议限制未下发（已由网关缓存托管）`
  }
  return '签名收发与回放状态正常'
})

const allFingerprints = computed(() => {
  const t = props.trace
  if (!t) return [] as { fp: string; role: 'outbound' | 'inbound' }[]
  const seen = new Set<string>()
  const list: { fp: string; role: 'outbound' | 'inbound' }[] = []
  for (const fp of t.upstream_out.fingerprints) {
    if (fp && !seen.has(`out:${fp}`)) {
      seen.add(`out:${fp}`)
      list.push({ fp, role: 'outbound' })
    }
  }
  const inFps = t.client_out.fingerprints.length
    ? t.client_out.fingerprints
    : t.upstream_in.fingerprints
  for (const fp of inFps) {
    if (fp && !seen.has(`in:${fp}`)) {
      seen.add(`in:${fp}`)
      list.push({ fp, role: 'inbound' })
    }
  }
  return list
})

const currentItems = computed<ThoughtSignatureItem[]>(() => {
  const t = props.trace
  if (!t) return []
  return t[activeList.value].items || []
})

function setActiveList(value: string) {
  activeList.value = value as (typeof activeList)['value']
}

const listTabs = computed(() => {
  const t = props.trace
  return [
    { id: 'client_in' as const, label: '客户端回传', count: t?.client_in.items.length ?? 0 },
    { id: 'upstream_out' as const, label: '发往上游', count: t?.upstream_out.items.length ?? 0 },
    { id: 'upstream_in' as const, label: '上游返回', count: t?.upstream_in.items.length ?? 0 },
    { id: 'client_out' as const, label: '下发客户端', count: t?.client_out.items.length ?? 0 },
  ]
})

function kindBadgeClass(kind: string): string {
  if (kind === 'real') return 'bg-emerald-50 text-emerald-700 border-emerald-200'
  if (kind === 'skip_sentinel') return 'bg-amber-50 text-amber-800 border-amber-200'
  if (kind === 'invalid') return 'bg-rose-50 text-rose-700 border-rose-200'
  return 'bg-slate-100 text-slate-600 border-slate-200'
}

function actionBadgeClass(action: string): string {
  if (action === 'warm_hit') return 'bg-sky-50 text-sky-700 border-sky-200'
  if (action === 'cold_hit') return 'bg-purple-50 text-purple-700 border-purple-200'
  if (action === 'skip_fill') return 'bg-amber-50 text-amber-800 border-amber-200'
  if (action === 'conflict') return 'bg-rose-50 text-rose-700 border-rose-200'
  return 'bg-slate-100 text-slate-600 border-slate-200'
}

function jumpToFingerprint(fp: string) {
  void router.push({
    path: '/logs',
    query: { category: 'thought_signature', search: fp },
  })
}

function jumpToRequestLogs() {
  void router.push({
    path: '/logs',
    query: { category: 'thought_signature', request_id: props.requestId },
  })
}
</script>

<template>
  <div
    v-if="!trace"
    class="rounded-xl border border-slate-200 bg-slate-50/70 p-8 text-center text-sm text-slate-500 space-y-1.5"
  >
    <p class="font-medium text-slate-700">
      此请求暂无思维签名（thoughtSignature）追踪数据
    </p>
    <p class="text-xs text-slate-400">
      旧版记录、尚未进入模型规范化阶段即被拦截的请求，或非生成类接口不产生签名追踪。
    </p>
  </div>

  <div
    v-else
    class="space-y-4 text-sm"
  >
    <!-- Top Summary Banner -->
    <div
      class="rounded-xl border p-3.5 flex flex-wrap items-center justify-between gap-3"
      :class="bannerClass"
    >
      <div class="space-y-0.5 min-w-0">
        <div class="flex flex-wrap items-center gap-2">
          <span class="font-semibold text-sm">{{ bannerTitle }}</span>
          <span class="px-2 py-0.5 rounded-md text-[11px] font-medium bg-white/80 border border-current/15">
            协议：{{ sigProtocolLabels[trace.protocol] || trace.protocol }}
          </span>
          <span
            v-if="trace.attempt > 1"
            class="px-2 py-0.5 rounded-md text-[11px] font-medium bg-white/80 border border-current/15"
          >
            第 {{ trace.attempt }} 次尝试
          </span>
        </div>
        <p class="text-xs opacity-80">
          仅记录 8 位短指纹、长度与载体类型，不保存提示词、工具参数或完整签名原文。
        </p>
      </div>
      <button
        type="button"
        class="shrink-0 px-3 py-1.5 rounded-lg text-xs font-medium bg-white/90 hover:bg-white border border-slate-300/80 text-slate-700 cursor-pointer transition-colors"
        @click="jumpToRequestLogs"
      >
        在运行日志查看 ↗
      </button>
    </div>

    <!-- Outbound Flow: Client -> Gateway Replay -> Upstream Wire -->
    <div class="space-y-2">
      <h3 class="text-xs font-semibold uppercase tracking-wide text-slate-500">
        出站链路（客户端回传 → 网关恢复/兜底 → 发往上游）
      </h3>
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3">
        <!-- 1. Client In -->
        <div class="rounded-xl border border-slate-200 bg-white p-3.5 space-y-2">
          <div class="flex items-center justify-between gap-2">
            <span class="text-xs font-semibold text-slate-800">1. 客户端回传</span>
            <span class="text-[11px] text-slate-400 tabular-nums">
              {{ trace.client_in.model_turns }} 个历史轮
            </span>
          </div>
          <dl class="grid grid-cols-2 gap-y-1.5 gap-x-2 text-xs">
            <dt class="text-slate-500">
              FC 签名
            </dt>
            <dd class="font-mono font-medium text-right tabular-nums">
              {{ trace.client_in.fc_signed }} / {{ trace.client_in.fc_total }}
              <span
                v-if="trace.client_in.fc_missing"
                class="text-amber-600"
              >(缺 {{ trace.client_in.fc_missing }})</span>
            </dd>
            <dt class="text-slate-500">
              思考签名
            </dt>
            <dd class="font-mono font-medium text-right tabular-nums">
              {{ trace.client_in.thought_signed }} / {{ trace.client_in.thought_total }}
            </dd>
            <dt class="text-slate-500">
              空尾 / 其他
            </dt>
            <dd class="font-mono font-medium text-right tabular-nums">
              {{ trace.client_in.tail_signed }} / {{ trace.client_in.other_signed }}
            </dd>
            <dt class="text-slate-500">
              非法/过滤丢弃
            </dt>
            <dd
              class="font-mono font-medium text-right tabular-nums"
              :class="trace.client_in.dropped > 0 ? 'text-rose-600' : 'text-slate-600'"
            >
              {{ trace.client_in.dropped }}
            </dd>
          </dl>
          <div
            v-if="trace.client_in.sources.length"
            class="flex flex-wrap gap-1 pt-1 border-t border-slate-100"
          >
            <span
              v-for="src in trace.client_in.sources"
              :key="src"
              class="px-1.5 py-0.5 rounded bg-slate-100 text-slate-600 text-[10px] font-mono"
            >{{ sigSourceLabels[src] || src }}</span>
          </div>
        </div>

        <!-- 2. Gateway Replay -->
        <div class="rounded-xl border border-slate-200 bg-white p-3.5 space-y-2">
          <div class="flex items-center justify-between gap-2">
            <span class="text-xs font-semibold text-slate-800">2. 网关缓存与兜底</span>
            <span
              class="px-1.5 py-0.5 rounded text-[10px] font-medium"
              :class="
                trace.replay.continuation_reused
                  ? 'bg-cyan-50 text-cyan-700'
                  : 'bg-slate-100 text-slate-500'
              "
            >
              {{ trace.replay.continuation_reused ? '续传令牌已复用' : '无续传令牌' }}
            </span>
          </div>
          <dl class="grid grid-cols-2 gap-y-1.5 gap-x-2 text-xs">
            <dt class="text-slate-500">
              温缓存命中
            </dt>
            <dd class="font-mono font-medium text-right tabular-nums text-sky-700">
              {{ trace.replay.warm_hits }} 轮
            </dd>
            <dt class="text-slate-500">
              冷缓存命中
            </dt>
            <dd class="font-mono font-medium text-right tabular-nums text-purple-700">
              {{ trace.replay.cold_hits }} 轮
            </dd>
            <dt class="text-slate-500">
              SKIP 兜底补填
            </dt>
            <dd
              class="font-mono font-medium text-right tabular-nums"
              :class="trace.replay.skip_fills > 0 ? 'text-amber-700' : 'text-slate-600'"
            >
              {{ trace.replay.skip_fills }} 次
            </dd>
            <dt class="text-slate-500">
              冲突 / 未命中
            </dt>
            <dd class="font-mono font-medium text-right tabular-nums">
              <span :class="trace.replay.conflicts > 0 ? 'text-rose-600' : 'text-slate-600'">
                {{ trace.replay.conflicts }}
              </span>
              / {{ trace.replay.misses }}
            </dd>
          </dl>
          <div
            v-if="trace.replay.miss_reasons.length"
            class="flex flex-wrap gap-1 pt-1 border-t border-slate-100"
          >
            <span
              v-for="reason in trace.replay.miss_reasons"
              :key="reason"
              class="px-1.5 py-0.5 rounded bg-amber-50 text-amber-800 text-[10px]"
              :title="reason"
            >{{ sigReasonLabels[reason] || reason }}</span>
          </div>
        </div>

        <!-- 3. Upstream Out -->
        <div class="rounded-xl border border-slate-200 bg-white p-3.5 space-y-2">
          <div class="flex items-center justify-between gap-2">
            <span class="text-xs font-semibold text-slate-800">3. 发往上游 (Slot 14)</span>
            <span
              v-if="bareLeadTurns > 0"
              class="px-1.5 py-0.5 rounded bg-rose-100 text-rose-700 text-[10px] font-semibold"
              title="每轮只有首个 functionCall 需要签名；此处只统计首 FC 裸发的轮次，兄弟 FC 不计"
            >
              400 风险
            </span>
          </div>
          <dl class="grid grid-cols-2 gap-y-1.5 gap-x-2 text-xs">
            <dt class="text-slate-500">
              真实签名
            </dt>
            <dd class="font-mono font-medium text-right tabular-nums text-emerald-700">
              {{ trace.upstream_out.real_sigs }}
            </dd>
            <dt class="text-slate-500">
              SKIP 哨兵
            </dt>
            <dd class="font-mono font-medium text-right tabular-nums text-amber-700">
              {{ trace.upstream_out.skip_sigs }}
            </dd>
            <dt class="text-slate-500">
              总 FC / 未签 FC
            </dt>
            <dd
              class="font-mono font-medium text-right tabular-nums"
              title="兄弟 functionCall 本来就不带签名，未签 FC 计数本身不代表风险；风险看首 FC 裸发轮次"
            >
              {{ trace.upstream_out.fc_total }} /
              <span
                :class="
                  bareLeadTurns > 0 ? 'text-rose-600 font-bold' : 'text-slate-600'
                "
              >
                {{ trace.upstream_out.fc_unsigned }}
              </span>
            </dd>
            <dt class="text-slate-500">
              槽 11 续传
            </dt>
            <dd class="font-mono font-medium text-right">
              {{ trace.upstream_out.continuation_sent ? '已发送' : '未发送' }}
            </dd>
          </dl>
        </div>
      </div>
    </div>

    <!-- Inbound Flow: Upstream In + Cache -> Client Out -->
    <div class="space-y-2">
      <h3 class="text-xs font-semibold uppercase tracking-wide text-slate-500">
        入站链路（Google 上游返回 → 网关缓存 → 下发客户端）
      </h3>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-3">
        <!-- 4. Upstream In -->
        <div class="rounded-xl border border-slate-200 bg-white p-3.5 space-y-2">
          <div class="flex items-center justify-between gap-2">
            <span class="text-xs font-semibold text-slate-800">4. 上游返回与缓存写入</span>
            <span
              class="px-1.5 py-0.5 rounded text-[10px] font-medium"
              :class="
                trace.upstream_in.cached_warm
                  ? 'bg-emerald-50 text-emerald-700'
                  : trace.upstream_in.cache_status === 'conflict'
                    ? 'bg-rose-50 text-rose-700'
                    : 'bg-slate-100 text-slate-500'
              "
            >
              温={{ trace.upstream_in.cached_warm ? '已存' : '未存' }} · 冷={{
                trace.upstream_in.cached_cold ? '已存' : '未存'
              }}
            </span>
          </div>
          <dl class="grid grid-cols-[auto_1fr] items-baseline gap-y-1.5 gap-x-3 text-xs">
            <dt class="text-slate-500 whitespace-nowrap">
              上游返回签名总数
            </dt>
            <dd class="font-mono font-medium text-right tabular-nums text-teal-700">
              {{ trace.upstream_in.total_sigs }}
            </dd>
            <dt class="text-slate-500 whitespace-nowrap">
              载体分布
            </dt>
            <dd class="flex flex-wrap justify-end gap-1 text-slate-700">
              <template v-if="Object.keys(trace.upstream_in.by_carrier).length">
                <span
                  v-for="(cnt, carrier) in trace.upstream_in.by_carrier"
                  :key="carrier"
                  class="px-1.5 py-0.5 rounded bg-slate-100 text-[11px] whitespace-nowrap"
                >{{ sigCarrierLabels[carrier] || carrier }} ×{{ cnt }}</span>
              </template>
              <span
                v-else
                class="text-slate-400"
              >无</span>
            </dd>
          </dl>
          <p
            v-if="trace.upstream_in.cache_note"
            class="text-[11px] text-slate-500 pt-1 border-t border-slate-100"
          >
            缓存备注：{{ sigReasonLabels[trace.upstream_in.cache_note] || trace.upstream_in.cache_note }}
          </p>
        </div>

        <!-- 5. Client Out -->
        <div class="rounded-xl border border-slate-200 bg-white p-3.5 space-y-2">
          <div class="flex items-center justify-between gap-2">
            <span class="text-xs font-semibold text-slate-800">5. 下发客户端</span>
            <span
              v-if="trace.client_out.dropped > 0"
              class="px-1.5 py-0.5 rounded bg-amber-50 text-amber-800 text-[10px] font-medium"
            >
              协议丢弃 {{ trace.client_out.dropped }} 条
            </span>
          </div>
          <dl class="grid grid-cols-[auto_1fr] items-baseline gap-y-1.5 gap-x-3 text-xs">
            <dt class="text-slate-500 whitespace-nowrap">
              下发签名数
            </dt>
            <dd class="font-mono font-medium text-right tabular-nums text-emerald-700">
              {{ trace.client_out.total_sigs }}
            </dd>
            <dt class="text-slate-500 whitespace-nowrap">
              下发字段载体
            </dt>
            <dd class="flex flex-wrap justify-end gap-1">
              <template v-if="trace.client_out.carriers.length">
                <span
                  v-for="carrier in trace.client_out.carriers"
                  :key="carrier"
                  class="px-1.5 py-0.5 rounded bg-slate-100 font-mono text-[11px] text-slate-700 whitespace-nowrap"
                >{{ sigSourceLabels[carrier] || carrier }}</span>
              </template>
              <span
                v-else
                class="text-slate-400"
              >无</span>
            </dd>
          </dl>
          <div
            v-if="trace.client_out.drop_reasons.length"
            class="flex flex-wrap gap-1 pt-1 border-t border-slate-100"
          >
            <span
              v-for="reason in trace.client_out.drop_reasons"
              :key="reason"
              class="px-1.5 py-0.5 rounded bg-amber-50 text-amber-800 text-[10px]"
              :title="reason"
            >{{ sigReasonLabels[reason] || reason }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- Clickable Fingerprints Bar -->
    <div
      v-if="allFingerprints.length"
      class="rounded-xl border border-slate-200 bg-slate-50/70 p-3 flex flex-wrap items-center gap-2 text-xs"
    >
      <span class="text-slate-500 font-medium shrink-0">签名短指纹追踪（点击跨轮查日志）：</span>
      <button
        v-for="item in allFingerprints"
        :key="`${item.role}:${item.fp}`"
        type="button"
        class="inline-flex items-center gap-1 px-2 py-0.5 rounded-md border font-mono text-[11px] cursor-pointer transition-colors"
        :class="
          item.role === 'outbound'
            ? 'bg-indigo-50 text-indigo-700 border-indigo-200 hover:bg-indigo-100'
            : 'bg-teal-50 text-teal-700 border-teal-200 hover:bg-teal-100'
        "
        @click="jumpToFingerprint(item.fp)"
      >
        <span>{{ item.role === 'outbound' ? '↑' : '↓' }}</span>
        <span>fp:{{ item.fp }}</span>
      </button>
    </div>

    <!-- Per-Turn Replay Table -->
    <div
      v-if="trace.replay.items.length"
      class="space-y-2"
    >
      <h3 class="text-xs font-semibold text-slate-600">
        分轮历史恢复明细（{{ trace.replay.items.length }} 轮）
      </h3>
      <div class="overflow-x-auto rounded-xl border border-slate-200">
        <table class="w-full text-left text-xs">
          <thead class="bg-slate-50 text-slate-500 border-b border-slate-200">
            <tr>
              <th class="px-3 py-2 font-medium">
                模型轮次
              </th>
              <th class="px-3 py-2 font-medium">
                恢复动作
              </th>
              <th class="px-3 py-2 font-medium text-right">
                FC 数
              </th>
              <th class="px-3 py-2 font-medium text-right">
                恢复/补填签名
              </th>
              <th class="px-3 py-2 font-medium">
                诊断说明
              </th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100 bg-white">
            <tr
              v-for="(item, idx) in trace.replay.items"
              :key="idx"
            >
              <td class="px-3 py-2 font-mono text-slate-700">
                Turn #{{ item.turn_index }}
              </td>
              <td class="px-3 py-2">
                <span
                  class="inline-flex items-center px-1.5 py-0.5 rounded border text-[11px] font-medium"
                  :class="actionBadgeClass(item.action)"
                >{{ sigActionLabels[item.action] || item.action }}</span>
              </td>
              <td class="px-3 py-2 font-mono text-right tabular-nums">
                {{ item.fc_count }}
              </td>
              <td class="px-3 py-2 font-mono text-right tabular-nums">
                {{ item.restored_sigs }}
              </td>
              <td class="px-3 py-2 text-slate-600">
                {{
                  item.reason
                    ? sigReasonLabels[item.reason] || item.reason
                    : item.continuation_reused
                      ? '精确匹配并复用续传令牌'
                      : '精确匹配'
                }}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>

    <!-- Detailed Signature Carrier Table -->
    <div class="space-y-2">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <h3 class="text-xs font-semibold text-slate-600">
          签名载体明细
        </h3>
        <SegmentedControl
          :model-value="activeList"
          :options="listTabs.map((t) => ({ value: t.id, label: t.label, count: t.count }))"
          count-style="muted"
          container-class="inline-flex rounded-lg bg-slate-100 p-0.5 text-xs"
          item-class="px-2.5 py-1 rounded-md cursor-pointer transition-colors"
          @update:model-value="setActiveList"
        />
      </div>

      <div
        v-if="!currentItems.length"
        class="rounded-xl border border-slate-200 bg-slate-50/50 py-6 text-center text-xs text-slate-400"
      >
        当前阶段无签名载体记录
      </div>
      <div
        v-else
        class="overflow-x-auto rounded-xl border border-slate-200"
      >
        <table class="w-full text-left text-xs">
          <thead class="bg-slate-50 text-slate-500 border-b border-slate-200">
            <tr>
              <th class="px-3 py-2 font-medium">
                载体
              </th>
              <th class="px-3 py-2 font-medium">
                状态
              </th>
              <th class="px-3 py-2 font-medium">
                指纹 / 长度
              </th>
              <th class="px-3 py-2 font-medium">
                来源 / 字段
              </th>
              <th class="px-3 py-2 font-medium">
                函数 / 备注
              </th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100 bg-white">
            <tr
              v-for="(item, idx) in currentItems"
              :key="idx"
            >
              <td class="px-3 py-2 whitespace-nowrap text-slate-800">
                <span>{{ sigCarrierLabels[item.carrier] || item.carrier }}</span>
                <span
                  v-if="item.turn_index"
                  class="ml-1 text-[10px] font-mono text-slate-400"
                >T{{ item.turn_index }}</span>
              </td>
              <td class="px-3 py-2 whitespace-nowrap">
                <span
                  class="inline-flex items-center px-1.5 py-0.5 rounded border text-[11px]"
                  :class="kindBadgeClass(item.kind)"
                >{{ sigKindLabels[item.kind] || item.kind }}</span>
              </td>
              <td class="px-3 py-2 whitespace-nowrap font-mono">
                <button
                  v-if="item.fp && item.fp !== 'SKIP'"
                  type="button"
                  class="text-brand-700 hover:underline cursor-pointer"
                  :title="`前缀：${item.head}…（点击追踪此指纹）`"
                  @click="jumpToFingerprint(item.fp)"
                >
                  {{ item.fp }}
                </button>
                <span
                  v-else
                  class="text-slate-400"
                >{{ item.fp || '—' }}</span>
                <span
                  v-if="item.len"
                  class="ml-1.5 text-[11px] text-slate-400"
                >({{ item.len }})</span>
              </td>
              <td class="px-3 py-2 font-mono text-[11px] text-slate-600">
                {{ item.source ? sigSourceLabels[item.source] || item.source : '—' }}
              </td>
              <td class="px-3 py-2 text-slate-600 break-words max-w-xs">
                <span
                  v-if="item.call_name"
                  class="font-mono text-slate-800"
                >{{ item.call_name }}</span>
                <span
                  v-if="item.call_id"
                  class="ml-1 font-mono text-[10px] text-slate-400"
                >({{ item.call_id.slice(0, 16) }})</span>
                <span
                  v-if="item.note"
                  class="block text-[11px] text-amber-700 mt-0.5"
                >{{ sigReasonLabels[item.note] || item.note }}</span>
                <span
                  v-if="!item.call_name && !item.note"
                  class="text-slate-400"
                >—</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</template>
