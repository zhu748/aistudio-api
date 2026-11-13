<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Info, TriangleAlert, CircleCheck, Wand2 } from 'lucide-vue-next'

/**
 * 代理地址输入框：类型下拉 + 粘贴智能识别 + 即时校验提示。
 *
 * - 粘贴含协议头的地址（含 RFC 7065 无斜线 turn: 形态）自动切换类型；
 * - 无协议简写按所选类型补全 scheme（如选 TURN 输入 1.2.3.4:3478 → turn://1.2.3.4:3478）；
 * - 「自动识别」下无协议的 host:port 原样提交（后端默认按 HTTP 处理）；
 * - SSTP 缺凭据 / SOCKS·TURN 缺端口等常见问题实时提示。
 */

type ProxyType = 'auto' | 'http' | 'https' | 'socks5' | 'socks5h' | 'turn' | 'turns' | 'sstp'

const props = withDefaults(
  defineProps<{
    modelValue: string
    placeholder?: string
  }>(),
  { placeholder: '' },
)

const emit = defineEmits<{
  'update:modelValue': [val: string]
}>()

const TYPE_OPTIONS: Array<{ value: ProxyType, label: string }> = [
  { value: 'auto', label: '自动识别' },
  { value: 'http', label: 'HTTP' },
  { value: 'https', label: 'HTTPS' },
  { value: 'socks5', label: 'SOCKS5' },
  { value: 'socks5h', label: 'SOCKS5 远程DNS' },
  { value: 'turn', label: 'TURN' },
  { value: 'turns', label: 'TURNS' },
  { value: 'sstp', label: 'SSTP' },
]

const TYPE_LABELS: Record<ProxyType, string> = {
  auto: '自动识别',
  http: 'HTTP',
  https: 'HTTPS',
  socks5: 'SOCKS5',
  socks5h: 'SOCKS5 远程DNS',
  turn: 'TURN',
  turns: 'TURNS',
  sstp: 'SSTP',
}

const selectedType = ref<ProxyType>('auto')
const text = ref('')

/** 从文本识别协议（支持 RFC 7065 无斜线 turn:/sstp: 形态）；识别不了返回 null。 */
function detectType(value: string): ProxyType | null {
  const m = value.trim().toLowerCase().match(/^(turns?|sstp|socks5h?|https?):/i)
  if (!m) return null
  const scheme = m[1]
  if (scheme === 'turn' || scheme === 'turns' || scheme === 'sstp') return scheme
  if (scheme === 'socks5h') return 'socks5h'
  if (scheme === 'socks5') return 'socks5'
  return scheme === 'https' ? 'https' : 'http'
}

/** 最终提交值：文本自带协议头时以文本为准，否则按所选类型补全。 */
const composed = computed(() => {
  const t = text.value.trim()
  if (!t) return ''
  if (detectType(t)) return t
  if (selectedType.value !== 'auto') return `${selectedType.value}://${t}`
  return t
})

watch(composed, (val) => {
  emit('update:modelValue', val)
})

// 粘贴/输入时自动切换类型（协议头优先于手动选择）
watch(text, (t) => {
  const detected = detectType(t)
  if (detected) selectedType.value = detected
})

// 外部赋值（打开弹窗回填账号已有代理）时拆解为「类型 + 文本」
watch(
  () => props.modelValue,
  (val) => {
    if ((val ?? '') === composed.value) return
    const t = (val ?? '').trim()
    selectedType.value = detectType(t) ?? 'auto'
    text.value = t
  },
)

interface Hint {
  kind: 'ok' | 'warn' | 'error'
  text: string
}

const hint = computed<Hint | null>(() => {
  const t = text.value.trim()
  if (!t) return null
  const lower = t.toLowerCase()
  if (lower.startsWith('socks4')) {
    return { kind: 'error', text: '暂不支持 SOCKS4 代理（可选 SOCKS5）' }
  }
  const generic = lower.match(/^([a-z][a-z0-9+.-]*):\/\//)
  const detected = detectType(t)
  if (generic && !detected) {
    return {
      kind: 'error',
      text: `暂不支持 ${generic[1].toUpperCase()} 协议，支持 HTTP/HTTPS/SOCKS5/TURN/TURNS/SSTP`,
    }
  }
  const scheme = detected ?? (selectedType.value !== 'auto' ? selectedType.value : null)
  if (!scheme) return null
  // RFC 7065 无斜线形态补斜线后才能被 URL 解析
  const parseable = composed.value.replace(/^(turns?|sstp):(?!\/\/)/i, '$1://')
  let parsed: URL
  try {
    parsed = new URL(parseable)
  } catch {
    return { kind: 'error', text: '地址格式无法解析，请检查端口与凭据写法' }
  }
  const port = parsed.port
  const user = parsed.username
  const password = parsed.password
  if (!port && scheme !== 'http' && scheme !== 'https') {
    const defaults: Record<string, string> = {
      socks5: '1080',
      socks5h: '1080',
      turn: '3478',
      turns: '5349',
      sstp: '443',
    }
    return {
      kind: 'warn',
      text: `请指定端口（默认 ${defaults[scheme] ?? ''}）`,
    }
  }
  if (scheme === 'sstp' && (!user || !password)) {
    return { kind: 'warn', text: 'SSTP（MS-CHAPv2）需要同时填写用户名和密码' }
  }
  if ((scheme === 'turn' || scheme === 'turns') && !user) {
    return { kind: 'ok', text: `已识别 ${TYPE_LABELS[scheme]} · 匿名分配服务器可直接使用` }
  }
  return { kind: 'ok', text: `已识别 ${TYPE_LABELS[scheme]} 代理` }
})
</script>

<template>
  <div class="space-y-1.5">
    <div class="flex gap-1.5">
      <select
        v-model="selectedType"
        aria-label="代理类型"
        class="shrink-0 px-2 py-2 bg-slate-50 border border-slate-200 rounded-lg text-xs font-medium text-slate-600 outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all cursor-pointer"
      >
        <option
          v-for="opt in TYPE_OPTIONS"
          :key="opt.value"
          :value="opt.value"
        >
          {{ opt.label }}
        </option>
      </select>
      <div class="relative flex-1">
        <Wand2 class="w-3.5 h-3.5 absolute left-2.5 top-1/2 -translate-y-1/2 text-slate-300 pointer-events-none" />
        <input
          v-model="text"
          type="text"
          autocomplete="off"
          spellcheck="false"
          :placeholder="placeholder"
          class="w-full pl-8 pr-3 py-2 bg-slate-50 border border-slate-200 rounded-lg text-xs font-mono outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
        >
      </div>
    </div>
    <p
      v-if="hint"
      class="text-[11px] leading-relaxed flex items-center gap-1"
      :class="{
        'text-emerald-600': hint.kind === 'ok',
        'text-amber-600': hint.kind === 'warn',
        'text-rose-600': hint.kind === 'error',
      }"
    >
      <CircleCheck
        v-if="hint.kind === 'ok'"
        class="w-3 h-3 shrink-0"
      />
      <TriangleAlert
        v-else-if="hint.kind === 'warn'"
        class="w-3 h-3 shrink-0"
      />
      <Info
        v-else
        class="w-3 h-3 shrink-0"
      />
      <span>{{ hint.text }}</span>
    </p>
  </div>
</template>
