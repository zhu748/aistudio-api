<script setup lang="ts">
import { ref } from 'vue'
import { useAccountsStore } from '@/stores/accounts.ts'
import Modal from '@/components/ui/Modal.vue'
import Button from '@/components/ui/Button.vue'
import ProxyInputField from '@/components/ui/ProxyInputField.vue'
import { Sparkles, FileText, Upload, FolderOpen, CheckCircle2 } from 'lucide-vue-next'

defineProps<{
  modelValue: boolean
}>()

const emit = defineEmits<{
  'update:modelValue': [val: boolean]
}>()

const accountsStore = useAccountsStore()

const activeTab = ref<'text' | 'file'>('text')

// Text paste state
const cookieInput = ref('')
const namePrefix = ref('')
const singleEmail = ref('')
const autoProbe = ref(true)
const proxyInput = ref('')

// File import state
const fileInputRef = ref<HTMLInputElement | null>(null)
const selectedFileName = ref('')
const selectedFileSize = ref('')
const selectedFileContent = ref('')
const localFilePath = ref('')
const detectedCount = ref<number | null>(null)

function onFileChange(event: Event) {
  const target = event.target as HTMLInputElement
  const file = target.files?.[0]
  if (!file) return

  selectedFileName.value = file.name
  selectedFileSize.value = `${(file.size / 1024).toFixed(1)} KB`

  const reader = new FileReader()
  reader.onload = (e) => {
    const text = (e.target?.result as string) || ''
    selectedFileContent.value = text
    try {
      const parsed = JSON.parse(text)
      if (Array.isArray(parsed)) {
        detectedCount.value = parsed.length
      } else if (Array.isArray(parsed.cookies)) {
        detectedCount.value = 1
      } else {
        const list = parsed.accounts || parsed.profiles || []
        detectedCount.value = Array.isArray(list) ? list.length : 1
      }
    } catch {
      detectedCount.value = null
    }
  }
  reader.readAsText(file)
}

function clearSelectedFile() {
  selectedFileName.value = ''
  selectedFileSize.value = ''
  selectedFileContent.value = ''
  detectedCount.value = null
  if (fileInputRef.value) {
    fileInputRef.value.value = ''
  }
}

async function handleSubmit() {
  if (activeTab.value === 'file') {
    const content = selectedFileContent.value.trim()
    const path = localFilePath.value.trim()
    if (!content && !path) return

    const ok = await accountsStore.importBundle({
      content: content || undefined,
      file_path: path || undefined,
    })
    if (ok) {
      clearSelectedFile()
      localFilePath.value = ''
      emit('update:modelValue', false)
    }
    return
  }

  const raw = cookieInput.value.trim()
  if (!raw) return

  if (raw.startsWith('{') && (raw.includes('"accounts"') || raw.includes('"bundle"'))) {
    const ok = await accountsStore.importBundle({ content: raw })
    if (ok) {
      cookieInput.value = ''
      namePrefix.value = ''
      singleEmail.value = ''
      emit('update:modelValue', false)
    }
    return
  }
  const proxy = proxyInput.value.trim() || undefined
  const ok = autoProbe.value
    ? await accountsStore.probeAndImport({
        cookies: raw,
        name_prefix: namePrefix.value.trim() || undefined,
        proxy_url: proxy,
      })
    : await accountsStore.importCookies({
        cookies: raw,
        name: namePrefix.value.trim() || undefined,
        email: singleEmail.value.trim() || undefined,
        proxy_url: proxy,
      })

  if (ok) {
    cookieInput.value = ''
    namePrefix.value = ''
    singleEmail.value = ''
    proxyInput.value = ''
    emit('update:modelValue', false)
  }
}
</script>

<template>
  <Modal
    :model-value="modelValue"
    max-width="lg"
    title="导入 Google 账号凭据"
    subtitle="支持单条 Cookie 递归探测多登录身份 (u/0, u/1) 或 JSON 凭据包批量导入"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="space-y-4">
      <!-- Tab Switcher -->
      <div class="flex items-center p-1 bg-slate-100 rounded-lg">
        <button
          type="button"
          class="flex-1 flex items-center justify-center gap-1.5 py-1.5 text-xs font-medium rounded-md transition-all cursor-pointer"
          :class="activeTab === 'text' ? 'bg-white text-slate-900 font-semibold shadow-2xs' : 'text-slate-500 hover:text-slate-900'"
          @click="activeTab = 'text'"
        >
          <FileText class="w-3.5 h-3.5" />
          <span>文本粘贴导入</span>
        </button>
        <button
          type="button"
          class="flex-1 flex items-center justify-center gap-1.5 py-1.5 text-xs font-medium rounded-md transition-all cursor-pointer"
          :class="activeTab === 'file' ? 'bg-white text-slate-900 font-semibold shadow-2xs' : 'text-slate-500 hover:text-slate-900'"
          @click="activeTab = 'file'"
        >
          <Upload class="w-3.5 h-3.5" />
          <span>文件批量导入</span>
        </button>
      </div>

      <!-- Mode: File Import -->
      <div
        v-if="activeTab === 'file'"
        class="space-y-4"
      >
        <div
          class="border border-dashed border-slate-300 hover:border-brand-500 bg-slate-50/70 hover:bg-brand-50/20 rounded-xl p-5 text-center transition-all cursor-pointer relative"
          @click="fileInputRef?.click()"
        >
          <input
            ref="fileInputRef"
            type="file"
            accept=".json,application/json"
            class="hidden"
            @change="onFileChange"
          >
          <div
            v-if="!selectedFileName"
            class="flex flex-col items-center gap-2"
          >
            <div class="w-9 h-9 rounded-lg bg-slate-900 text-white flex items-center justify-center">
              <Upload class="w-4 h-4 text-brand-400" />
            </div>
            <div>
              <p class="text-xs font-semibold text-slate-800">
                点击选择凭据包文件 (.json)
              </p>
              <p class="text-[11px] text-slate-400 mt-0.5">
                支持批量账号凭据包 (.json) 或 Cookie JSON 数组文件
              </p>
            </div>
          </div>

          <div
            v-else
            class="flex items-center justify-between bg-white p-3 rounded-lg border border-slate-200"
          >
            <div class="flex items-center gap-2.5 text-left">
              <div class="w-8 h-8 rounded-lg bg-emerald-50 text-emerald-600 border border-emerald-200/70 flex items-center justify-center">
                <CheckCircle2 class="w-4 h-4" />
              </div>
              <div>
                <p class="text-xs font-semibold text-slate-800 font-mono">
                  {{ selectedFileName }}
                </p>
                <p class="text-[11px] text-slate-400 tabular-nums">
                  {{ selectedFileSize }}
                  <span
                    v-if="detectedCount !== null"
                    class="ml-1 text-brand-600 font-medium"
                  >· 识别到 {{ detectedCount }} 个账号</span>
                </p>
              </div>
            </div>
            <button
              type="button"
              class="text-xs text-slate-400 hover:text-rose-600 px-2 py-1 cursor-pointer"
              @click.stop="clearSelectedFile"
            >
              移除
            </button>
          </div>
        </div>

        <div class="space-y-1.5">
          <label class="text-xs font-semibold text-slate-700 flex items-center gap-1.5">
            <FolderOpen class="w-3.5 h-3.5 text-slate-400" />
            <span>或读取服务端 data/ 目录下的 JSON 文件</span>
          </label>
          <input
            v-model="localFilePath"
            type="text"
            placeholder="data/aistudio_accounts.json"
            class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg text-xs font-mono outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
          >
          <p class="text-[11px] text-slate-400 leading-relaxed">
            仅可读取服务端 <code class="font-mono">data/</code> 目录（或 <code class="font-mono">AISTUDIO_IMPORT_ROOTS</code> 白名单目录）下的 .json 文件。
          </p>
        </div>
      </div>

      <!-- Mode: Text Paste -->
      <div
        v-else
        class="space-y-3.5"
      >
        <div class="space-y-1.5">
          <label class="text-xs font-semibold text-slate-700 flex items-center justify-between">
            <span>Cookie 凭据内容</span>
            <span class="text-[11px] text-slate-400 font-normal">支持 JSON 数组、Netscape 或 Key=Value 格式</span>
          </label>
          <textarea
            v-model="cookieInput"
            rows="5"
            placeholder="[ { &quot;name&quot;: &quot;SAPISID&quot;, &quot;value&quot;: &quot;...&quot; }, ... ] 或&#10;SID=xxx; SSID=xxx; HSID=xxx; SAPISID=xxx; ..."
            class="w-full p-3 font-mono text-xs bg-slate-50 border border-slate-200 rounded-lg outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
          />
        </div>

        <div class="space-y-1.5">
          <label class="text-xs font-semibold text-slate-700 flex items-center justify-between">
            <span>独立出口代理 <span class="text-slate-400 font-normal">(可选)</span></span>
            <span class="text-[11px] text-slate-400 font-normal">粘贴自动识别类型，也可直接附在 Cookie 文本首尾</span>
          </label>
          <ProxyInputField
            v-model="proxyInput"
            placeholder="socks5://user:pass@127.0.0.1:1080、127.0.0.1:7890 或 turn:host:3478"
          />
        </div>

        <!-- Auto Probe Toggle -->
        <label class="flex items-start gap-2.5 p-3 bg-brand-50/50 rounded-lg border border-brand-200/70 cursor-pointer select-none">
          <input
            v-model="autoProbe"
            type="checkbox"
            class="mt-0.5 rounded accent-brand-600 cursor-pointer"
          >
          <div class="flex-1 text-xs">
            <div class="font-semibold text-slate-900 flex items-center gap-1.5">
              <Sparkles class="w-3.5 h-3.5 text-brand-600" />
              <span>自动递归探测多身份子账号 (u/0, u/1, u/2…)</span>
            </div>
            <div class="text-slate-500 mt-0.5 text-[11px]">
              针对包含多个 Google 登录会话的 Cookie，自动探测全部有效子账号并独立建档。
            </div>
          </div>
        </label>

        <!-- Account Name / Email Inputs -->
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-3">
          <div class="space-y-1.5">
            <label class="block text-xs font-semibold text-slate-700">
              名称前缀 <span class="text-slate-400 font-normal">(可选)</span>
            </label>
            <input
              v-model="namePrefix"
              type="text"
              placeholder="Google Account"
              class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg text-xs outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
            >
          </div>

          <div
            v-if="!autoProbe"
            class="space-y-1.5"
          >
            <label class="block text-xs font-semibold text-slate-700">
              邮箱地址 <span class="text-slate-400 font-normal">(可选)</span>
            </label>
            <input
              v-model="singleEmail"
              type="email"
              placeholder="user@gmail.com"
              class="w-full px-3 py-2 bg-slate-50 border border-slate-200 rounded-lg text-xs outline-none focus:bg-white focus:border-brand-500 focus:ring-2 focus:ring-brand-500/20 transition-all"
            >
          </div>
        </div>
      </div>
    </div>

    <template #footer>
      <Button
        variant="ghost"
        size="sm"
        :disabled="accountsStore.importing"
        @click="emit('update:modelValue', false)"
      >
        取消
      </Button>
      <Button
        variant="primary"
        size="sm"
        :loading="accountsStore.importing"
        :disabled="activeTab === 'file' ? (!selectedFileContent && !localFilePath.trim()) : !cookieInput.trim()"
        @click="handleSubmit"
      >
        <span v-if="activeTab === 'file'">确认批量导入</span>
        <span v-else-if="autoProbe">探测并导入</span>
        <span v-else>导入 Cookie</span>
      </Button>
    </template>
  </Modal>
</template>
