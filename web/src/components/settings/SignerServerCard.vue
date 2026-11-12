<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { systemApi } from '@/api/system.ts'
import { useToastStore } from '@/stores/toast.ts'
import { useConfirm } from '@/composables/useConfirm.ts'
import type { SignerServerItem } from '@/types/system.ts'
import Modal from '@/components/ui/Modal.vue'
import Button from '@/components/ui/Button.vue'
import {
  KeyRound,
  CheckCircle2,
  XCircle,
  Plus,
  Trash2,
  Edit2,
  Activity,
  Check,
  Loader2,
  Settings2,
  RotateCcw,
} from 'lucide-vue-next'

const emit = defineEmits<{
  (e: 'switched'): void
}>()

const toast = useToastStore()
const { confirm } = useConfirm()

const loading = ref(false)
const switching = ref<string | null>(null)
const testingId = ref<string | null>(null)
const resetting = ref(false)
const busy = computed(() => switching.value !== null || resetting.value || testingId.value !== null)
const testResults = ref<Record<string, { ok: boolean; ms?: number; err?: string }>>({})

const activeServerId = ref('local_spidermonkey')
const envOverride = ref<string | null>(null)
const runtimeOverride = ref<string | null>(null)
const servers = ref<SignerServerItem[]>([])

const showResetButton = computed(() => {
  if (!runtimeOverride.value) return false
  const defaultId = envOverride.value
    ?? servers.value.find((s) => s.id === activeServerId.value)?.id
    ?? activeServerId.value
  return runtimeOverride.value !== defaultId
})

const showModal = ref(false)
const isEditing = ref(false)
const saving = ref(false)

const form = ref<Partial<SignerServerItem>>({
  id: '',
  name: '',
  description: '',
  url: '',
  user_agent: '',
  headers: {},
})
const customHeadersText = ref('')

async function fetchServers() {
  loading.value = true
  try {
    const res = await systemApi.getSignerServers()
    activeServerId.value = res.active_server
    envOverride.value = res.env_override ?? null
    runtimeOverride.value = res.runtime_override ?? null
    servers.value = res.servers
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '获取签名服务器列表失败'
    toast.error(msg)
  } finally {
    loading.value = false
  }
}

async function handleSwitch(serverId: string) {
  if (serverId === activeServerId.value) return
  switching.value = serverId
  try {
    const res = await systemApi.switchSignerServer(serverId)
    const activeId = res.active_server.id
    activeServerId.value = activeId
    runtimeOverride.value = res.runtime_override ?? null
    servers.value = servers.value.map((s) => ({
      ...s,
      is_active: s.id === activeId,
    }))
    delete testResults.value[serverId]
    if (res.ok && activeId === serverId) {
      if (res.note) {
        toast.info(res.note, 8000)
      } else {
        toast.success(`已切换至签名节点: ${res.active_server.name}`)
      }
    } else {
      toast.error(res.error || `切换未成功，当前激活节点: ${res.active_server.name}`)
    }
    emit('switched')
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '切换签名服务器失败'
    toast.error(msg)
  } finally {
    switching.value = null
  }
}

async function handleReset() {
  resetting.value = true
  try {
    const res = await systemApi.resetSignerServer()
    await fetchServers()
    if (res.ok) {
      if (res.changed) {
        toast.success(`已恢复默认签名节点: ${res.active_server.name}`)
      } else {
        toast.info('当前未设置运行时覆盖，节点保持默认')
      }
    } else {
      toast.error(res.error || `恢复默认节点失败，当前激活节点: ${res.active_server.name}`)
    }
    emit('switched')
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '恢复默认节点失败'
    toast.error(msg)
  } finally {
    resetting.value = false
  }
}

async function handleTest(server: SignerServerItem) {
  testingId.value = server.id
  try {
    const res = await systemApi.testSignerServer(server.url)
    if (res.ok) {
      testResults.value[server.id] = { ok: true, ms: res.elapsed_ms }
      toast.success(`${server.name}: 连通正常 (${res.elapsed_ms}ms)`)
    } else {
      const errMsg = res.error || `HTTP ${res.status_code || '异常'}`
      testResults.value[server.id] = { ok: false, ms: res.elapsed_ms, err: errMsg }
      toast.error(`${server.name}: 连通失败 - ${errMsg}`)
    }
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '请求异常'
    testResults.value[server.id] = { ok: false, err: msg }
    toast.error(`${server.name}: 连通异常 - ${msg}`)
  } finally {
    testingId.value = null
  }
}

function openCreateModal() {
  isEditing.value = false
  form.value = {
    id: '',
    name: '',
    description: '',
    url: 'http://127.0.0.1:8081',
    user_agent: 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:152.0) Gecko/20100101 Firefox/152.0',
    headers: { 'X-User-Agent': 'grpc-web-javascript/0.1' },
  }
  customHeadersText.value = '{"X-User-Agent": "grpc-web-javascript/0.1"}'
  showModal.value = true
}

function openEditModal(server: SignerServerItem) {
  isEditing.value = true
  form.value = { ...server }
  customHeadersText.value = server.headers ? JSON.stringify(server.headers, null, 2) : '{}'
  showModal.value = true
}

async function handleSaveServer() {
  if (!form.value.name?.trim() || !form.value.url?.trim()) {
    toast.error('节点名称与服务地址为必填项')
    return
  }

  let parsedHeaders: Record<string, string> = {}
  if (customHeadersText.value.trim()) {
    try {
      parsedHeaders = JSON.parse(customHeadersText.value.trim())
    } catch {
      toast.error('自定义请求头必须是合法的 JSON 键值对')
      return
    }
  }

  saving.value = true
  try {
    const payload: Partial<SignerServerItem> = {
      id: form.value.id || undefined,
      name: form.value.name.trim(),
      description: form.value.description?.trim() || '',
      url: form.value.url.trim(),
      user_agent: form.value.user_agent?.trim() || '',
      auth_token_env: form.value.auth_token_env?.trim() || '',
      headers: parsedHeaders,
    }
    await systemApi.upsertSignerServer(payload)
    toast.success('签名服务器配置已保存')
    showModal.value = false
    await fetchServers()
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '保存失败'
    toast.error(msg)
  } finally {
    saving.value = false
  }
}

async function handleDeleteServer(server: SignerServerItem) {
  if (server.id === activeServerId.value) {
    toast.error('无法删除当前处于激活状态的签名服务器')
    return
  }
  const ok = await confirm({
    title: '删除签名服务节点',
    message: `确认删除签名服务节点「${server.name}」吗？`,
    confirmText: '确认删除',
    variant: 'danger',
  })
  if (!ok) return

  try {
    await systemApi.deleteSignerServer(server.id)
    toast.success('已删除签名服务节点')
    await fetchServers()
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : '删除失败'
    toast.error(msg)
  }
}

onMounted(() => {
  void fetchServers()
})
</script>

<template>
  <div class="bg-white border border-slate-200/90 rounded-xl p-5 shadow-2xs space-y-4">
    <!-- Header -->
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 pb-4 border-b border-slate-100">
      <div class="flex items-start gap-2.5">
        <div class="w-8 h-8 rounded-lg bg-slate-900 text-brand-400 flex items-center justify-center shrink-0 mt-0.5">
          <KeyRound class="w-4 h-4" />
        </div>
        <div>
          <div class="flex items-center gap-2 flex-wrap">
            <h3 class="text-sm font-semibold text-slate-900">
              WAA 证明签名服务与特征绑定
            </h3>
            <span class="px-2 py-0.5 text-[10px] font-semibold bg-slate-100 text-slate-600 rounded">
              手动切换
            </span>
          </div>
          <p class="text-[11px] text-slate-400 mt-0.5">
            管理无头 WAA 签名节点（SpiderMonkey / NodeVM）。切换节点时自动对齐绑定的 User-Agent 与指纹请求头，防止 403 拦截。
          </p>
        </div>
      </div>

      <Button
        variant="secondary"
        size="sm"
        class="self-start sm:self-auto shrink-0"
        @click="openCreateModal"
      >
        <Plus class="w-3.5 h-3.5" />
        <span>添加签名节点</span>
      </Button>
    </div>

    <!-- Env Default / Runtime Override Banner -->
    <div
      v-if="envOverride"
      class="flex items-start gap-2.5 px-3.5 py-2.5 rounded-lg bg-sky-50/80 border border-sky-200/80"
    >
      <Settings2 class="w-4 h-4 text-sky-600 shrink-0 mt-0.5" />
      <div class="text-xs text-sky-900 leading-relaxed flex-1">
        启动默认节点由环境变量 <code class="font-mono font-semibold bg-white/80 px-1 py-0.5 rounded border border-sky-200">AISTUDIO_WAA_SERVER={{ envOverride }}</code> 指定。面板手动切换对当前进程即时生效，重启后自动恢复该默认。
        <button
          v-if="showResetButton"
          type="button"
          class="inline-flex items-center gap-1 ml-2 px-2 py-0.5 text-[11px] font-semibold text-sky-700 bg-white hover:bg-sky-50 rounded border border-sky-200 transition-colors cursor-pointer disabled:opacity-50"
          :disabled="busy"
          @click="handleReset"
        >
          <RotateCcw
            v-if="!resetting"
            class="w-3 h-3"
          />
          <Loader2
            v-else
            class="w-3 h-3 animate-spin"
          />
          恢复默认节点
        </button>
      </div>
    </div>
    <div
      v-else-if="runtimeOverride"
      class="flex items-start gap-2.5 px-3.5 py-2.5 rounded-lg bg-sky-50/80 border border-sky-200/80"
    >
      <Settings2 class="w-4 h-4 text-sky-600 shrink-0 mt-0.5" />
      <div class="text-xs text-sky-900 leading-relaxed flex-1">
        当前节点由面板手动切换指定（运行时覆盖），重启后恢复配置文件默认。
        <button
          type="button"
          class="inline-flex items-center gap-1 ml-2 px-2 py-0.5 text-[11px] font-semibold text-sky-700 bg-white hover:bg-sky-50 rounded border border-sky-200 transition-colors cursor-pointer disabled:opacity-50"
          :disabled="busy"
          @click="handleReset"
        >
          <RotateCcw
            v-if="!resetting"
            class="w-3 h-3"
          />
          <Loader2
            v-else
            class="w-3 h-3 animate-spin"
          />
          恢复默认节点
        </button>
      </div>
    </div>

    <!-- Server List -->
    <div class="space-y-3">
      <div
        v-for="server in servers"
        :key="server.id"
        class="p-4 rounded-xl border transition-all"
        :class="[
          server.is_active
            ? 'border-brand-400 bg-brand-50/20 ring-1 ring-brand-500/15'
            : 'border-slate-200/90 bg-white hover:border-slate-300',
        ]"
      >
        <div class="flex flex-col md:flex-row md:items-center justify-between gap-3">
          <!-- Info -->
          <div class="space-y-1.5 min-w-0">
            <div class="flex items-center gap-2 flex-wrap">
              <span class="text-xs font-bold text-slate-900">
                {{ server.name }}
              </span>
              <span
                v-if="server.is_active"
                class="inline-flex items-center gap-1 px-2 py-0.5 text-[10px] font-semibold bg-emerald-50 text-emerald-700 border border-emerald-200 rounded-md"
              >
                <Check class="w-3 h-3" />
                当前激活
              </span>
              <span
                v-if="server.is_env_default"
                class="inline-flex items-center gap-1 px-2 py-0.5 text-[10px] font-semibold bg-sky-50 text-sky-700 border border-sky-200 rounded-md"
                title="环境变量 AISTUDIO_WAA_SERVER 指定的启动默认节点"
              >
                <Settings2 class="w-3 h-3" />
                环境变量默认
              </span>
              <span class="text-[11px] font-mono text-slate-600 bg-slate-100 border border-slate-200/70 px-1.5 py-0.5 rounded">
                {{ server.url }}
              </span>
              <!-- Test Result Indicator -->
              <span
                v-if="testResults[server.id]"
                class="inline-flex items-center gap-1 text-[11px] font-mono tabular-nums px-1.5 py-0.5 rounded border"
                :class="
                  testResults[server.id].ok
                    ? 'bg-emerald-50 text-emerald-700 border-emerald-200'
                    : 'bg-rose-50 text-rose-700 border-rose-200'
                "
              >
                <CheckCircle2
                  v-if="testResults[server.id].ok"
                  class="w-3 h-3"
                />
                <XCircle
                  v-else
                  class="w-3 h-3"
                />
                {{ testResults[server.id].ok ? `${testResults[server.id].ms}ms` : '连接失败' }}
              </span>
            </div>

            <p
              v-if="server.description"
              class="text-xs text-slate-500"
            >
              {{ server.description }}
            </p>

            <!-- Bound User-Agent & Features -->
            <div class="text-[11px] text-slate-600 bg-slate-50 p-2.5 rounded-lg border border-slate-200/70 space-y-1 font-mono break-all">
              <div>
                <span class="text-slate-400 select-none">User-Agent: </span>
                <span>{{ server.user_agent || '未设置 (跟随全局默认)' }}</span>
              </div>
              <div v-if="server.headers && Object.keys(server.headers).length > 0">
                <span class="text-slate-400 select-none">绑定请求头: </span>
                <span>{{ Object.entries(server.headers).map(([k, v]) => `${k}: ${v}`).join('; ') }}</span>
              </div>
            </div>
          </div>

          <!-- Actions -->
          <div class="flex items-center gap-1.5 shrink-0 self-end md:self-center">
            <Button
              variant="secondary"
              size="xs"
              :loading="testingId === server.id"
              :disabled="busy"
              @click="handleTest(server)"
            >
              <Activity
                v-if="testingId !== server.id"
                class="w-3 h-3 text-slate-500"
              />
              <span>测试连通</span>
            </Button>

            <Button
              v-if="!server.is_active"
              variant="primary"
              size="xs"
              :loading="switching === server.id"
              :disabled="busy"
              @click="handleSwitch(server.id)"
            >
              <span>切换至此节点</span>
            </Button>

            <button
              type="button"
              class="p-1.5 text-slate-400 hover:text-slate-700 hover:bg-slate-100 rounded-lg transition-colors cursor-pointer"
              title="编辑节点配置"
              @click="openEditModal(server)"
            >
              <Edit2 class="w-3.5 h-3.5" />
            </button>

            <button
              v-if="!server.is_active"
              type="button"
              class="p-1.5 text-slate-400 hover:text-rose-600 hover:bg-rose-50 rounded-lg transition-colors cursor-pointer"
              title="删除节点"
              @click="handleDeleteServer(server)"
            >
              <Trash2 class="w-3.5 h-3.5" />
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Edit/Add Modal (Unified Modal Component) -->
    <Modal
      v-model="showModal"
      max-width="lg"
      :title="isEditing ? '编辑 WAA 签名服务节点' : '添加 WAA 签名服务节点'"
      subtitle="配置签名服务 HTTP 地址及其对应的浏览器指纹请求头"
    >
      <div class="space-y-3.5 text-xs">
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div class="space-y-1.5">
            <label class="block font-semibold text-slate-700">节点标识 (ID)</label>
            <input
              v-model="form.id"
              type="text"
              :disabled="isEditing"
              placeholder="如 local_spidermonkey"
              class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg font-mono outline-none focus:bg-white focus:border-brand-500 disabled:opacity-60"
            >
          </div>

          <div class="space-y-1.5">
            <label class="block font-semibold text-slate-700">节点名称 *</label>
            <input
              v-model="form.name"
              type="text"
              placeholder="如 本地 Go 签名服务"
              class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg outline-none focus:bg-white focus:border-brand-500"
            >
          </div>
        </div>

        <div class="space-y-1.5">
          <label class="block font-semibold text-slate-700">服务地址 (URL) *</label>
          <input
            v-model="form.url"
            type="text"
            placeholder="http://127.0.0.1:8081"
            class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg font-mono outline-none focus:bg-white focus:border-brand-500"
          >
        </div>

        <div class="space-y-1.5">
          <label class="block font-semibold text-slate-700">绑定的特征 User-Agent</label>
          <textarea
            v-model="form.user_agent"
            rows="2"
            placeholder="填写该签名机底层模拟的真实浏览器 User-Agent"
            class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg font-mono text-[11px] outline-none focus:bg-white focus:border-brand-500"
          />
          <p class="text-[11px] text-slate-400">
            SpiderMonkey 152 对应 Firefox 152 UA；NodeVM 对应其模拟的 Chromium UA。
          </p>
        </div>

        <div class="space-y-1.5">
          <label class="block font-semibold text-slate-700">自定义特征请求头 (JSON)</label>
          <textarea
            v-model="customHeadersText"
            rows="3"
            :placeholder="JSON.stringify({ 'X-User-Agent': 'grpc-web-javascript/0.1' })"
            class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg font-mono text-[11px] outline-none focus:bg-white focus:border-brand-500"
          />
        </div>

        <div class="space-y-1.5">
          <label class="block font-semibold text-slate-700">备注说明</label>
          <input
            v-model="form.description"
            type="text"
            placeholder="选填，关于该签名节点的补充说明"
            class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg outline-none focus:bg-white focus:border-brand-500"
          >
        </div>
        <div class="space-y-1.5">
          <label class="block font-semibold text-slate-700">鉴权环境变量名</label>
          <input
            v-model="form.auth_token_env"
            placeholder="例如 MY_SIGNER_TOKEN（填写变量名）"
            class="w-full px-3 py-2 border border-slate-200 rounded-lg font-mono"
          >
          <p class="text-slate-400">
            节点需要共享密钥时，先在服务端设置环境变量，再填写变量名。凭据只发送到此节点。
          </p>
        </div>
      </div>

      <template #footer>
        <Button
          variant="ghost"
          size="sm"
          @click="showModal = false"
        >
          取消
        </Button>
        <Button
          variant="primary"
          size="sm"
          :loading="saving"
          @click="handleSaveServer"
        >
          保存节点
        </Button>
      </template>
    </Modal>
  </div>
</template>
