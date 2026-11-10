<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { systemApi } from '@/api/system.ts'
import { useToastStore } from '@/stores/toast.ts'
import type { SystemConfig } from '@/types/system.ts'
import ApiKeyManagerCard from '@/components/settings/ApiKeyManagerCard.vue'
import LogConfigCard from '@/components/settings/LogConfigCard.vue'
import VisualConfigEditor from '@/components/config/VisualConfigEditor.vue'
import SignerServerCard from '@/components/settings/SignerServerCard.vue'
import AntiTruncationCard from '@/components/settings/AntiTruncationCard.vue'
import {
  Server,
  CheckCircle2,
  AlertCircle,
  Cpu,
  Globe,
  Lock,
  Activity,
  KeyRound,
  Key,
  SlidersHorizontal,
} from 'lucide-vue-next'

const toast = useToastStore()

const config = ref<SystemConfig | null>(null)
const loading = ref(false)

type SettingsTab = 'general' | 'transport' | 'apikeys' | 'models'
const activeTab = ref<SettingsTab>('general')

const tabs: { key: SettingsTab; label: string; desc: string; icon: typeof Activity }[] = [
  { key: 'general', label: '日志与调试', desc: '日志级别与请求报文转储', icon: Activity },
  { key: 'transport', label: '签名与抗截断', desc: 'WAA 签名引擎与抗截断传输', icon: KeyRound },
  { key: 'apikeys', label: '客户端密钥', desc: '入站 API Keys 鉴权管理', icon: Key },
  { key: 'models', label: '模型与协议规则', desc: 'OpenAI/Claude 映射与 YAML', icon: SlidersHorizontal },
]

async function loadConfig() {
  loading.value = true
  try {
    config.value = await systemApi.getConfig()
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '获取系统配置失败'
    toast.error(msg)
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void loadConfig()
})
</script>

<template>
  <div class="space-y-5">
    <!-- Compact Runtime Telemetry Bar -->
    <div class="grid grid-cols-2 lg:grid-cols-4 gap-3.5">
      <div class="bg-white border border-slate-200/90 rounded-xl p-3.5 shadow-2xs flex items-center justify-between">
        <div>
          <div class="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">
            监听端口
          </div>
          <div class="text-base font-bold text-slate-900 font-mono tabular-nums mt-0.5">
            :{{ config?.port || 8080 }}
          </div>
        </div>
        <div class="w-8 h-8 rounded-lg bg-slate-100 text-slate-600 flex items-center justify-center">
          <Server class="w-4 h-4" />
        </div>
      </div>

      <div class="bg-white border border-slate-200/90 rounded-xl p-3.5 shadow-2xs flex items-center justify-between">
        <div>
          <div class="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">
            日志级别
          </div>
          <div class="flex items-baseline gap-2 mt-0.5">
            <span class="text-base font-bold text-slate-900 font-mono">{{ config?.log_level || 'INFO' }}</span>
            <span class="text-[11px] text-slate-400">{{ config?.dump_to_file ? '· 落盘开' : '· 仅终端' }}</span>
          </div>
        </div>
        <div class="w-8 h-8 rounded-lg bg-brand-50 text-brand-600 flex items-center justify-center">
          <Cpu class="w-4 h-4" />
        </div>
      </div>

      <div class="bg-white border border-slate-200/90 rounded-xl p-3.5 shadow-2xs flex items-center justify-between">
        <div>
          <div class="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">
            全局出口代理
          </div>
          <div class="text-sm font-bold mt-0.5">
            <span
              v-if="config?.proxy_configured"
              class="text-emerald-600 flex items-center gap-1"
            >
              <CheckCircle2 class="w-3.5 h-3.5" /> 已启用代理
            </span>
            <span
              v-else
              class="text-slate-600"
            >直连模式</span>
          </div>
        </div>
        <div class="w-8 h-8 rounded-lg bg-sky-50 text-sky-600 flex items-center justify-center">
          <Globe class="w-4 h-4" />
        </div>
      </div>

      <div class="bg-white border border-slate-200/90 rounded-xl p-3.5 shadow-2xs flex items-center justify-between">
        <div>
          <div class="text-[11px] font-semibold text-slate-400 uppercase tracking-wider">
            控制台鉴权
          </div>
          <div class="text-sm font-bold mt-0.5">
            <span
              v-if="config?.auth_enabled"
              class="text-emerald-600 flex items-center gap-1"
            >
              <CheckCircle2 class="w-3.5 h-3.5" /> 密码已保护
            </span>
            <span
              v-else
              class="text-amber-600 flex items-center gap-1"
            >
              <AlertCircle class="w-3.5 h-3.5" /> 免密访问
            </span>
          </div>
        </div>
        <div class="w-8 h-8 rounded-lg bg-slate-100 text-slate-600 flex items-center justify-center">
          <Lock class="w-4 h-4" />
        </div>
      </div>
    </div>

    <!-- Section Sub-Navigation Tabs -->
    <div class="bg-white border border-slate-200/90 rounded-xl p-1.5 shadow-2xs flex items-center gap-1 overflow-x-auto">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        type="button"
        class="flex-1 min-w-[140px] flex items-center justify-center sm:justify-start gap-2.5 px-3.5 py-2.5 rounded-lg text-left transition-all cursor-pointer"
        :class="
          activeTab === tab.key
            ? 'bg-slate-900 text-white shadow-2xs'
            : 'text-slate-600 hover:bg-slate-50 hover:text-slate-900'
        "
        @click="activeTab = tab.key"
      >
        <component
          :is="tab.icon"
          class="w-4 h-4 shrink-0"
          :class="activeTab === tab.key ? 'text-brand-400' : 'text-slate-400'"
        />
        <div class="min-w-0">
          <div class="text-xs font-semibold leading-tight truncate">
            {{ tab.label }}
          </div>
          <div
            class="hidden md:block text-[10px] truncate mt-0.5"
            :class="activeTab === tab.key ? 'text-slate-300' : 'text-slate-400'"
          >
            {{ tab.desc }}
          </div>
        </div>
      </button>
    </div>

    <!-- Tab 1: General & Logging -->
    <div
      v-if="activeTab === 'general'"
      class="space-y-4"
    >
      <div
        v-if="config && !config.auth_enabled"
        class="px-4 py-3 bg-amber-50/80 border border-amber-200/80 rounded-xl flex items-center justify-between gap-3 text-xs text-amber-900"
      >
        <div class="flex items-center gap-2">
          <AlertCircle class="w-4 h-4 text-amber-600 shrink-0" />
          <span>控制台当前处于免密访问模式。公网部署建议设置环境变量 <code class="font-mono font-semibold bg-white/80 px-1.5 py-0.5 rounded border border-amber-200">AISTUDIO_WEB_PASSWORD</code> 开启后台密码保护。</span>
        </div>
      </div>

      <LogConfigCard
        v-if="config"
        :initial-config="config"
        @saved="loadConfig"
      />
    </div>

    <!-- Tab 2: WAA Signer & Anti-Truncation Transport -->
    <div
      v-else-if="activeTab === 'transport'"
      class="space-y-5"
    >
      <SignerServerCard @switched="loadConfig" />
      <AntiTruncationCard />
    </div>

    <!-- Tab 3: Client API Keys -->
    <div v-else-if="activeTab === 'apikeys'">
      <ApiKeyManagerCard />
    </div>

    <!-- Tab 4: Model Rules, Protocol Compat & YAML -->
    <div v-else-if="activeTab === 'models'">
      <VisualConfigEditor
        v-if="config"
        :initial-yaml="config.yaml_content"
        @saved="loadConfig"
      />
    </div>
  </div>
</template>
