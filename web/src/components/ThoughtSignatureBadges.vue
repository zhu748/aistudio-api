<script setup lang="ts">
import type { ThoughtSignatureLogMeta } from '@/types/system.ts'
import { sigProtocolLabels } from '@/utils/requests.ts'

defineProps<{
  meta: ThoughtSignatureLogMeta
}>()

const emit = defineEmits<{
  filterFp: [fp: string]
}>()
</script>

<template>
  <div class="flex flex-wrap items-center gap-1.5 mt-1 mb-0.5 text-[11px] font-sans">
    <span
      class="inline-flex items-center px-1.5 py-px rounded border font-semibold"
      :class="
        meta.direction === 'outbound'
          ? 'bg-indigo-500/20 text-indigo-300 border-indigo-400/30'
          : 'bg-teal-500/20 text-teal-300 border-teal-400/30'
      "
    >
      {{ meta.direction === 'outbound' ? '出站 ↑' : '入站 ↓' }}
    </span>

    <span class="inline-flex items-center px-1.5 py-px rounded border bg-slate-800 text-slate-300 border-slate-700">
      {{ sigProtocolLabels[meta.protocol] || meta.protocol }}
    </span>

    <template v-if="meta.direction === 'outbound'">
      <span
        v-if="(meta.client_model_turns || 0) === 0"
        class="inline-flex items-center px-1.5 py-px rounded border bg-slate-800/80 text-slate-400 border-slate-700"
      >
        首轮无历史
      </span>
      <template v-else>
        <span
          v-if="(meta.client_fc_total || 0) > 0"
          class="inline-flex items-center px-1.5 py-px rounded border"
          :class="
            (meta.client_fc_unsigned_lead || 0) > 0
              ? 'bg-amber-500/15 text-amber-300 border-amber-500/30'
              : 'bg-slate-800/80 text-slate-300 border-slate-700'
          "
          title="所有带签名的工具调用占比。同批后续调用通常不带签名。"
        >
          回传签名 {{ meta.client_fc_signed || 0 }}/{{ meta.client_fc_total }}
          <template v-if="(meta.client_fc_unsigned_lead || 0) > 0">
            · 首调用缺签 {{ meta.client_fc_unsigned_lead }} 轮
          </template>
        </span>
        <span
          v-if="(meta.client_thought_signed || 0) > 0"
          class="inline-flex items-center px-1.5 py-px rounded border bg-emerald-500/15 text-emerald-300 border-emerald-500/30"
        >
          思考签 ×{{ meta.client_thought_signed }}
        </span>
        <span
          v-if="(meta.client_tail_signed || 0) > 0"
          class="inline-flex items-center px-1.5 py-px rounded border bg-emerald-500/15 text-emerald-300 border-emerald-500/30"
        >
          尾签 ×{{ meta.client_tail_signed }}
        </span>
      </template>

      <span
        v-if="(meta.warm_hits || 0) > 0"
        class="inline-flex items-center px-1.5 py-px rounded border bg-sky-500/15 text-sky-300 border-sky-500/30"
      >
        温缓存 ×{{ meta.warm_hits }}
      </span>
      <span
        v-if="(meta.cold_hits || 0) > 0"
        class="inline-flex items-center px-1.5 py-px rounded border bg-purple-500/15 text-purple-300 border-purple-500/30"
      >
        冷缓存 ×{{ meta.cold_hits }}
      </span>
      <span
        v-if="(meta.skip_fills || 0) > 0"
        class="inline-flex items-center px-1.5 py-px rounded border bg-amber-500/15 text-amber-300 border-amber-500/30"
      >
        SKIP兜底 ×{{ meta.skip_fills }}
      </span>
      <span
        v-if="meta.continuation_sent ?? meta.continuation_reused"
        class="inline-flex items-center px-1.5 py-px rounded border bg-cyan-500/15 text-cyan-300 border-cyan-500/30"
      >
        {{ meta.continuation_sent ? '已发送续传令牌' : '历史令牌命中' }}
      </span>
      <span
        v-if="(meta.upstream_fc_unsigned_lead || 0) > 0"
        class="inline-flex items-center px-1.5 py-px rounded border bg-rose-500/20 text-rose-300 border-rose-500/40 font-semibold"
        title="每轮只有首个 functionCall 需要签名；此处只统计首 FC 裸发的轮次，兄弟 FC 不计"
      >
        未签FC风险 ×{{ meta.upstream_fc_unsigned_lead }}
      </span>
      <span
        v-if="(meta.conflicts || 0) > 0"
        class="inline-flex items-center px-1.5 py-px rounded border bg-rose-500/15 text-rose-300 border-rose-500/35"
      >
        冲突 ×{{ meta.conflicts }}
      </span>
      <span
        v-if="(meta.client_dropped || 0) > 0"
        class="inline-flex items-center px-1.5 py-px rounded border bg-amber-500/15 text-amber-300 border-amber-500/35"
      >
        入站丢弃 ×{{ meta.client_dropped }}
      </span>
    </template>

    <template v-else>
      <span
        class="inline-flex items-center px-1.5 py-px rounded border"
        :class="
          (meta.upstream_total || 0) > 0
            ? 'bg-teal-500/15 text-teal-300 border-teal-500/30'
            : 'bg-slate-800/80 text-slate-400 border-slate-700'
        "
      >
        上游签 {{ meta.upstream_total || 0 }}
      </span>
      <span
        v-if="meta.cached_warm || meta.cached_cold"
        class="inline-flex items-center px-1.5 py-px rounded border bg-emerald-500/15 text-emerald-300 border-emerald-500/30"
      >
        已缓存 {{ meta.cached_warm && meta.cached_cold ? '温+冷' : meta.cached_warm ? '温' : '冷' }}
      </span>
      <span
        v-else-if="meta.cache_status === 'conflict'"
        class="inline-flex items-center px-1.5 py-px rounded border bg-rose-500/15 text-rose-300 border-rose-500/30"
      >
        缓存冲突
      </span>
      <span
        class="inline-flex items-center px-1.5 py-px rounded border"
        :class="
          (meta.client_total || 0) > 0 && (meta.client_total || 0) >= (meta.upstream_total || 0)
            ? 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30'
            : (meta.client_total || 0) > 0
              ? 'bg-amber-500/15 text-amber-300 border-amber-500/30'
              : 'bg-slate-800/80 text-slate-400 border-slate-700'
        "
      >
        下发客户端 {{ meta.client_total || 0 }}
      </span>
      <span
        v-if="(meta.client_dropped || 0) > 0"
        class="inline-flex items-center px-1.5 py-px rounded border bg-amber-500/15 text-amber-300 border-amber-500/30"
      >
        协议丢弃 ×{{ meta.client_dropped }}
      </span>
    </template>

    <button
      v-for="fp in (meta.fingerprints || []).slice(0, 4)"
      :key="fp"
      type="button"
      class="inline-flex items-center px-1.5 py-px rounded border font-mono text-[10px] bg-slate-800/90 text-brand-300 border-slate-700 hover:bg-brand-500/20 hover:border-brand-400/50 transition-colors cursor-pointer"
      title="点击按此 8 位签名指纹过滤日志，追踪跨轮回传闭环"
      @click="emit('filterFp', fp)"
    >
      fp:{{ fp }}
    </button>
  </div>
</template>
