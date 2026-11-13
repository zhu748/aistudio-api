<script setup lang="ts">
import { ref, watch } from 'vue'
import { systemApi } from '@/api/system.ts'
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
const saving = ref(false)

watch(
  () => props.initialConfig,
  (cfg) => {
    logLevel.value = cfg.log_level || 'INFO'
    dumpRequests.value = Boolean(cfg.dump_requests)
    dumpToFile.value = Boolean(cfg.dump_to_file)
    dumpDir.value = cfg.dump_dir || 'dumps'
    debugEnvActive.value = Boolean(cfg.debug_env_active)
  },
  { deep: true },
)

const logLevels = [
  { value: 'DEBUG', label: '调试 (DEBUG)', description: '输出详细底层跟踪日志' },
  { value: 'INFO', label: '标准 (INFO)', description: '日常运行状态与请求记录' },
  { value: 'WARNING', label: '警告 (WARNING)', description: '仅输出告警与异常' },
  { value: 'ERROR', label: '错误 (ERROR)', description: '仅记录故障错误' },
]

async function handleSave() {
  saving.value = true
  try {
    const res = await systemApi.updateLoggingConfig({
      level: logLevel.value,
      dump_requests: dumpRequests.value,
      dump_to_file: dumpToFile.value,
      dump_dir: dumpDir.value,
    })
    logLevel.value = res.log_level
    dumpRequests.value = res.dump_requests
    dumpToFile.value = res.dump_to_file
    dumpDir.value = res.dump_dir
    debugEnvActive.value = res.debug_env_active
    toast.success('日志与转储配置已更新')
    emit('saved')
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '保存配置失败'
    toast.error(msg)
  } finally {
    saving.value = false
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
  </div>
</template>
