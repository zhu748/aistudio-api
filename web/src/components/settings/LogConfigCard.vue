<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { systemApi } from '@/api/system.ts'
import { operationsApi } from '@/api/operations.ts'
import { useToastStore } from '@/stores/toast.ts'
import type { SystemConfig } from '@/types/system.ts'
import Button from '@/components/ui/Button.vue'
import ConfigSelect from '@/components/config/ConfigSelect.vue'
import ConfigSwitch from '@/components/config/ConfigSwitch.vue'
import ConfigInput from '@/components/config/ConfigInput.vue'
import {
  Activity,
  Save,
  Bug,
  Info,
  HardDrive,
  Folder,
  SlidersHorizontal,
  Terminal,
  Camera,
  Trash2,
} from 'lucide-vue-next'

const props = defineProps<{
  initialConfig: SystemConfig
}>()

const emit = defineEmits<{
  saved: []
}>()

const toast = useToastStore()

const logLevel = ref(props.initialConfig.log_level || 'INFO')
const dumpRequests = ref(Boolean(props.initialConfig.dump_requests))
const dumpToFile = ref(Boolean(props.initialConfig.dump_to_file))
const dumpDir = ref(props.initialConfig.dump_dir || 'dumps')
const debugEnvActive = ref(Boolean(props.initialConfig.debug_env_active))
const snapshotKeep = ref(String(props.initialConfig.upstream_snapshot_keep ?? 10))
const snapshotStats = ref<{ success: number; error: number; bytes: number } | null>(null)
const snapshotClearing = ref('')
const saving = ref(false)

watch(
  () => props.initialConfig,
  (cfg) => {
    logLevel.value = cfg.log_level || 'INFO'
    dumpRequests.value = Boolean(cfg.dump_requests)
    dumpToFile.value = Boolean(cfg.dump_to_file)
    dumpDir.value = cfg.dump_dir || 'dumps'
    debugEnvActive.value = Boolean(cfg.debug_env_active)
    snapshotKeep.value = String(cfg.upstream_snapshot_keep ?? 10)
  },
  { deep: true },
)

async function loadSnapshotStats() {
  try {
    snapshotStats.value = await operationsApi.upstreamSnapshotStats()
  } catch {
    snapshotStats.value = null
  }
}

onMounted(() => {
  void loadSnapshotStats()
})

const logLevels = [
  { value: 'DEBUG', label: '调试 (DEBUG)', description: '输出详细底层跟踪日志' },
  { value: 'INFO', label: '标准 (INFO)', description: '日常运行状态与请求记录' },
  { value: 'WARNING', label: '警告 (WARNING)', description: '仅输出告警与异常' },
  { value: 'ERROR', label: '错误 (ERROR)', description: '仅记录故障错误' },
]

async function handleSave() {
  saving.value = true
  try {
    const keep = Number.parseInt(snapshotKeep.value, 10)
    if (Number.isNaN(keep) || keep < 0 || keep > 1000) {
      toast.error('上游快照保留条数必须是 0-1000 之间的整数')
      return
    }
    const res = await systemApi.updateLoggingConfig({
      level: logLevel.value,
      dump_requests: dumpRequests.value,
      dump_to_file: dumpToFile.value,
      dump_dir: dumpDir.value,
      upstream_snapshot_keep: keep,
    })
    logLevel.value = res.log_level
    dumpRequests.value = res.dump_requests
    dumpToFile.value = res.dump_to_file
    dumpDir.value = res.dump_dir
    debugEnvActive.value = res.debug_env_active
    snapshotKeep.value = String(res.upstream_snapshot_keep)
    toast.success('日志与转储配置已更新')
    emit('saved')
    void loadSnapshotStats()
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '保存配置失败'
    toast.error(msg)
  } finally {
    saving.value = false
  }
}

async function handleClearSnapshots(scope: 'all' | 'success') {
  snapshotClearing.value = scope
  try {
    const res = await operationsApi.clearUpstreamSnapshots(scope)
    toast.success(
      `已清理快照：成功 ${res.removed.success} 条${scope === 'all' ? `，错误 ${res.removed.error} 条` : ''}`,
    )
    void loadSnapshotStats()
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '清理快照失败'
    toast.error(msg)
  } finally {
    snapshotClearing.value = ''
  }
}
</script>

<template>
  <div class="bg-white border border-slate-200/90 rounded-xl p-5 shadow-2xs space-y-5">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-100 pb-4">
      <div class="flex items-center gap-2.5">
        <div class="w-8 h-8 rounded-lg bg-slate-900 text-brand-400 flex items-center justify-center shrink-0">
          <Activity class="w-4 h-4" />
        </div>
        <div>
          <h3 class="font-semibold text-slate-900 text-sm flex items-center gap-2">
            <span>日志输出与报文转储</span>
            <span
              v-if="debugEnvActive"
              class="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-[10px] font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200"
            >
              <Bug class="w-3 h-3" /> DEBUG 环境变量生效中
            </span>
          </h3>
          <p class="text-[11px] text-slate-400 mt-0.5">
            控制服务端实时日志级别及上下游原始请求报文的落盘调试策略
          </p>
        </div>
      </div>

      <div class="flex items-center gap-2">
        <Button
          variant="primary"
          size="sm"
          :loading="saving"
          @click="handleSave"
        >
          <Save class="w-3.5 h-3.5" />
          <span>保存日志配置</span>
        </Button>
      </div>
    </div>

    <!-- Env variable hint banner if DEBUG is active -->
    <div
      v-if="debugEnvActive"
      class="p-3 bg-sky-50/80 border border-sky-200/80 rounded-lg flex items-start gap-2.5 text-xs text-sky-900 leading-relaxed"
    >
      <Info class="w-4 h-4 text-sky-600 shrink-0 mt-0.5" />
      <div>
        <span class="font-semibold">DEBUG 环境变量提示：</span>
        系统已设置 <code class="font-mono">DEBUG=true</code> 环境变量，请求报文转储已强制激活。日志输出级别仍遵循下方配置。
      </div>
    </div>

    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
      <!-- 1. Log Level Configuration -->
      <div class="p-4 bg-slate-50/70 border border-slate-200/80 rounded-xl flex flex-col justify-between gap-2.5">
        <div class="flex items-center gap-1.5 text-xs font-semibold text-slate-700">
          <SlidersHorizontal class="w-3.5 h-3.5 text-brand-600" />
          <span>全局日志等级</span>
        </div>
        <ConfigSelect
          :model-value="logLevel"
          label="日志输出级别"
          :options="logLevels"
          @update:model-value="logLevel = String($event || 'INFO')"
        />
      </div>

      <!-- 2. Request Dump Toggle -->
      <div class="p-4 bg-slate-50/70 border border-slate-200/80 rounded-xl flex flex-col justify-between gap-2.5">
        <div class="flex items-center gap-1.5 text-xs font-semibold text-slate-700">
          <Terminal class="w-3.5 h-3.5 text-brand-600" />
          <span>实时终端调试</span>
        </div>
        <ConfigSwitch
          :model-value="dumpRequests"
          label="控制台报文转储"
          description="在标准输出中打印完整的上下游请求与响应报文"
          @update:model-value="dumpRequests = $event"
        />
      </div>

      <!-- 3. Dump To File Switch -->
      <div class="p-4 bg-slate-50/70 border border-slate-200/80 rounded-xl flex flex-col justify-between gap-2.5">
        <div class="flex items-center gap-1.5 text-xs font-semibold text-slate-700">
          <HardDrive class="w-3.5 h-3.5 text-brand-600" />
          <span>报文持久化落盘</span>
        </div>
        <ConfigSwitch
          :model-value="dumpToFile"
          label="将报文保存为磁盘文件"
          description="将每次请求与响应写入独立调试文件，便于排查协议问题"
          @update:model-value="dumpToFile = $event"
        />
      </div>

      <!-- 4. Dump Directory Input -->
      <div class="p-4 bg-slate-50/70 border border-slate-200/80 rounded-xl flex flex-col justify-between gap-2.5">
        <div class="flex items-center gap-1.5 text-xs font-semibold text-slate-700">
          <Folder class="w-3.5 h-3.5 text-brand-600" />
          <span>转储文件存储目录</span>
        </div>
        <ConfigInput
          :model-value="dumpDir"
          label="相对或绝对目录路径"
          placeholder="dumps"
          @update:model-value="dumpDir = String($event || 'dumps')"
        />
      </div>
    </div>

    <!-- 5. Upstream Exchange Snapshots -->
    <div class="p-4 bg-slate-50/70 border border-slate-200/80 rounded-xl space-y-3">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="flex items-center gap-1.5 text-xs font-semibold text-slate-700">
          <Camera class="w-3.5 h-3.5 text-brand-600" />
          <span>上游交换快照（含 200 成功请求）</span>
        </div>
        <div class="flex items-center gap-2">
          <Button
            size="xs"
            variant="ghost"
            :loading="snapshotClearing === 'success'"
            :disabled="!snapshotStats?.success"
            @click="handleClearSnapshots('success')"
          >
            <Trash2 class="w-3.5 h-3.5" />仅清成功
          </Button>
          <Button
            size="xs"
            variant="ghost"
            :loading="snapshotClearing === 'all'"
            :disabled="!snapshotStats || (!snapshotStats.success && !snapshotStats.error)"
            @click="handleClearSnapshots('all')"
          >
            <Trash2 class="w-3.5 h-3.5" />全部清理
          </Button>
        </div>
      </div>
      <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        <ConfigInput
          :model-value="snapshotKeep"
          type="number"
          label="成功快照保留条数（随保存日志配置生效）"
          placeholder="10"
          description="0 = 不采集成功快照；错误快照不受此限制"
          @update:model-value="snapshotKeep = String($event ?? '10')"
        />
        <div class="text-[11px] text-slate-400 leading-relaxed self-end">
          <template v-if="snapshotStats">
            当前库存：成功 {{ snapshotStats.success }} 条 · 错误 {{ snapshotStats.error }} 条（不受条数限制）· 占用
            {{ (snapshotStats.bytes / 1024).toFixed(1) }} KiB
          </template>
          <template v-else>
            正在获取快照库存…
          </template>
        </div>
      </div>
      <p class="text-[11px] text-slate-400 leading-relaxed">
        每次模型调用的实际线缆请求（URL、有序请求头、wire 请求体、响应头与响应样本）均落盘为快照，
        成功与失败都可在请求详情页查看/下载；凭据头仅保留长度，WAA proof 截断存储。
      </p>
    </div>
  </div>
</template>
