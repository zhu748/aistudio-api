<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { systemApi } from '@/api/system.ts'
import { useToastStore } from '@/stores/toast.ts'
import Button from '@/components/ui/Button.vue'
import ConfigSwitch from '@/components/config/ConfigSwitch.vue'
import {
  HeartPulse,
  Save,
  Info,
  ShieldAlert,
  ExternalLink,
} from 'lucide-vue-next'

const toast = useToastStore()

const loading = ref(false)
const saving = ref(false)

const keepaliveUrl = ref('')
const interval = ref(60)
const allowPrivate = ref(false)
const running = ref(false)
const envOverrides = ref<Record<string, string>>({})

const hasEnvOverride = computed(() => Object.keys(envOverrides.value).length > 0)

const intervalHint = computed(() => {
  if (interval.value < 30) return '间隔较短，出站请求较频繁'
  if (interval.value > 600) return '间隔较长，防冻结效果可能不足'
  return ''
})

async function loadConfig() {
  loading.value = true
  try {
    const res = await systemApi.getKeepaliveConfig()
    keepaliveUrl.value = res.url
    interval.value = res.interval
    allowPrivate.value = res.allow_private
    running.value = res.running
    envOverrides.value = res.env_overrides || {}
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '获取保活配置失败'
    toast.error(msg)
  } finally {
    loading.value = false
  }
}

async function handleSave() {
  saving.value = true
  try {
    const res = await systemApi.updateKeepaliveConfig({
      url: keepaliveUrl.value,
      interval: interval.value,
      allow_private: allowPrivate.value,
    })
    keepaliveUrl.value = res.url
    interval.value = res.interval
    allowPrivate.value = res.allow_private
    running.value = res.running
    toast.success(
      res.url
        ? '保活配置已保存，保活任务已按新配置重启'
        : '保活配置已保存（URL 为空，保活任务已停止）',
    )
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '保存保活配置失败'
    toast.error(msg)
  } finally {
    saving.value = false
  }
}

onMounted(() => {
  void loadConfig()
})
</script>

<template>
  <div class="bg-white border border-slate-200/90 rounded-xl p-5 shadow-2xs space-y-5">
    <!-- Card Header -->
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-100 pb-4">
      <div class="flex items-center gap-2.5">
        <div class="w-8 h-8 rounded-lg bg-slate-900 text-brand-400 flex items-center justify-center shrink-0">
          <HeartPulse class="w-4 h-4" />
        </div>
        <div>
          <h3 class="font-semibold text-slate-900 text-sm">
            出站保活 (Outbound Keepalive)
          </h3>
          <p class="text-[11px] text-slate-400 mt-0.5">
            定期 GET 目标 URL 防免费托管平台冻结闲置实例；URL 留空则不启动
          </p>
        </div>
      </div>

      <div class="flex items-center gap-2">
        <span
          class="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[11px] font-semibold border"
          :class="running
            ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
            : 'bg-slate-50 text-slate-500 border-slate-200'"
        >
          <HeartPulse class="w-3 h-3" />
          {{ running ? '运行中' : '未运行' }}
        </span>
        <Button
          variant="primary"
          size="sm"
          :loading="saving || loading"
          @click="handleSave"
        >
          <Save class="w-3.5 h-3.5" />
          <span>保存并生效</span>
        </Button>
      </div>
    </div>

    <!-- Env Override Notice -->
    <div
      v-if="hasEnvOverride"
      class="p-3 bg-amber-50/80 border border-amber-200/80 rounded-lg flex items-start gap-2.5 text-xs text-amber-900 leading-relaxed"
    >
      <ShieldAlert class="w-4 h-4 text-amber-600 shrink-0 mt-0.5" />
      <div>
        检测到环境变量覆盖（{{
          Object.values(envOverrides).join(' / ')
        }}）：运行时以环境变量值为准，面板保存仅写入 config.yaml，重启后仍以环境变量优先。
      </div>
    </div>

    <!-- Usage Hint -->
    <div class="p-3 bg-brand-50/60 border border-brand-200/70 rounded-lg flex items-start gap-2.5 text-xs text-slate-700 leading-relaxed">
      <Info class="w-4 h-4 text-brand-600 shrink-0 mt-0.5" />
      <div>
        典型用法：Render / HuggingFace Space 等免费平台会冻结闲置实例，填本服务自身的
        <code class="font-mono px-1 py-0.5 rounded bg-white border border-brand-200/70 text-brand-700">https://&lt;域名&gt;/healthz</code>
        地址定期自我唤醒。保存后立即按新配置重启保活任务，无需重启服务。
      </div>
    </div>

    <!-- URL Input -->
    <div class="p-4 bg-slate-50/70 border border-slate-200/80 rounded-xl space-y-2.5">
      <label class="text-xs font-semibold text-slate-700 flex items-center gap-1.5">
        <ExternalLink class="w-3.5 h-3.5 text-brand-600" />
        <span>保活目标 URL（http/https，留空 = 不启动）</span>
      </label>
      <input
        v-model="keepaliveUrl"
        type="text"
        placeholder="https://your-app.onrender.com/healthz"
        class="w-full px-3 py-1.5 text-xs bg-white border border-slate-200 rounded-lg font-mono focus:outline-none focus:ring-2 focus:ring-brand-500/20 focus:border-brand-500 transition-all"
      >
    </div>

    <!-- Interval Slider -->
    <div class="p-4 bg-slate-50/70 border border-slate-200/80 rounded-xl space-y-3">
      <div class="flex items-center justify-between gap-3">
        <label class="text-xs font-semibold text-slate-700">请求间隔</label>
        <span class="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[11px] font-mono tabular-nums font-semibold bg-brand-50 text-brand-700 border border-brand-200/70">
          {{ interval }}s
        </span>
      </div>
      <input
        v-model.number="interval"
        type="range"
        min="5"
        max="600"
        step="5"
        class="w-full accent-brand-600 cursor-pointer"
      >
      <p
        v-if="intervalHint"
        class="text-[11px] text-slate-400"
      >
        {{ intervalHint }}
      </p>
    </div>

    <!-- Private Target Switch -->
    <div class="p-3.5 bg-slate-50/70 border border-slate-200/80 rounded-xl">
      <ConfigSwitch
        :model-value="allowPrivate"
        label="允许私网 / 环回目标"
        description="保活目标为 127.0.0.1 / 内网地址时需开启（默认拒绝，防被劫持会话探测内网）"
        @update:model-value="allowPrivate = $event"
      />
    </div>
  </div>
</template>
