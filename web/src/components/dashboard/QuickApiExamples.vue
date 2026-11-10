<script setup lang="ts">
import { ref, computed } from 'vue'
import { Code2, Terminal, Copy, Check } from 'lucide-vue-next'
import { useClipboard } from '@/composables/useClipboard.ts'

const { copied, copy } = useClipboard()

type ProtocolTab = 'gemini' | 'openai' | 'claude'
const activeProtocol = ref<ProtocolTab>('gemini')

const protocolTabs: { key: ProtocolTab; label: string; endpoint: string }[] = [
  { key: 'gemini', label: 'Gemini 原生协议', endpoint: '/v1beta' },
  { key: 'openai', label: 'OpenAI 兼容协议', endpoint: '/v1/chat/completions' },
  { key: 'claude', label: 'Claude 兼容协议', endpoint: '/v1/messages' },
]

const baseUrl = computed(() => {
  if (typeof window !== 'undefined' && window.location.origin) {
    return window.location.origin
  }
  return 'http://localhost:8080'
})

const currentSnippets = computed(() => {
  if (activeProtocol.value === 'openai') {
    return [
      {
        id: 'openai-curl',
        title: 'cURL (OpenAI /v1/chat/completions)',
        code: `curl -X POST "${baseUrl.value}/v1/chat/completions" \\
  -H "Authorization: Bearer your-api-key" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "gemini-3.7-flash",
    "messages": [{"role": "user", "content": "Hello!"}],
    "stream": true
  }'`,
      },
      {
        id: 'openai-py',
        title: 'Python (OpenAI SDK)',
        code: `from openai import OpenAI

client = OpenAI(
    api_key="your-api-key",
    base_url="${baseUrl.value}/v1",
)

resp = client.chat.completions.create(
    model="gemini-3.7-flash",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)`,
      },
    ]
  }

  if (activeProtocol.value === 'claude') {
    return [
      {
        id: 'claude-curl',
        title: 'cURL (Anthropic /v1/messages)',
        code: `curl -X POST "${baseUrl.value}/v1/messages" \\
  -H "x-api-key: your-api-key" \\
  -H "anthropic-version: 2023-06-01" \\
  -H "Content-Type: application/json" \\
  -d '{
    "model": "gemini-3.7-flash",
    "max_tokens": 1024,
    "messages": [{"role": "user", "content": "Hello!"}]
  }'`,
      },
      {
        id: 'claude-sdk',
        title: 'Python (Anthropic SDK / Claude Code)',
        code: `import anthropic

client = anthropic.Anthropic(
    api_key="your-api-key",
    base_url="${baseUrl.value}",
)

msg = client.messages.create(
    model="gemini-3.7-flash",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}],
)
print(msg.content[0].text)`,
      },
    ]
  }

  return [
    {
      id: 'gemini-curl',
      title: 'cURL (Gemini /v1beta)',
      code: `curl -X POST \\
  "${baseUrl.value}/v1beta/models/gemini-3.7-flash:generateContent" \\
  -H "x-goog-api-key: your-api-key" \\
  -H "Content-Type: application/json" \\
  -d '{
    "contents": [{"role": "user", "parts": [{"text": "Hello!"}]}]
  }'`,
    },
    {
      id: 'gemini-py',
      title: 'Python (google-genai SDK)',
      code: `from google import genai

client = genai.Client(
    api_key="your-api-key",
    http_options={
        "api_version": "v1beta",
        "base_url": "${baseUrl.value}",
    },
)

response = client.models.generate_content(
    model="gemini-3.7-flash",
    contents="Hello!",
)
print(response.text)`,
    },
  ]
})

function handleCopy(code: string, id: string) {
  void copy(code, id, '已复制调用示例到剪贴板')
}
</script>

<template>
  <div class="bg-white border border-slate-200/90 rounded-xl p-5 shadow-2xs space-y-4">
    <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-3 border-b border-slate-100 pb-3.5">
      <div class="flex items-center gap-2">
        <Code2 class="w-4 h-4 text-brand-600" />
        <h3 class="font-semibold text-slate-900 text-sm">
          多协议接入示例
        </h3>
      </div>

      <!-- Protocol Switcher Tabs -->
      <div class="flex items-center bg-slate-100 p-1 rounded-lg text-xs font-medium flex-wrap">
        <button
          v-for="tab in protocolTabs"
          :key="tab.key"
          type="button"
          class="px-2.5 py-1 rounded-md transition-all flex items-center gap-1.5 cursor-pointer"
          :class="
            activeProtocol === tab.key
              ? 'bg-white text-slate-900 font-semibold shadow-2xs'
              : 'text-slate-500 hover:text-slate-900'
          "
          @click="activeProtocol = tab.key"
        >
          <span>{{ tab.label }}</span>
          <span
            class="hidden md:inline text-[10px] font-mono px-1 py-px rounded"
            :class="activeProtocol === tab.key ? 'bg-brand-50 text-brand-700' : 'text-slate-400'"
          >
            {{ tab.endpoint }}
          </span>
        </button>
      </div>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-2 gap-4">
      <div
        v-for="snippet in currentSnippets"
        :key="snippet.id"
        class="bg-slate-950 text-slate-100 rounded-xl p-4 flex flex-col overflow-hidden border border-slate-800"
      >
        <div class="flex items-center justify-between pb-2.5 border-b border-slate-800/90 text-xs font-medium text-slate-400">
          <div class="flex items-center gap-1.5 font-mono">
            <Terminal class="w-3.5 h-3.5 text-brand-400" />
            <span>{{ snippet.title }}</span>
          </div>
          <button
            type="button"
            class="flex items-center gap-1 text-[11px] text-slate-300 hover:text-white px-2 py-0.5 rounded bg-slate-800 hover:bg-slate-700 transition-colors cursor-pointer"
            @click="handleCopy(snippet.code, snippet.id)"
          >
            <Check
              v-if="copied === snippet.id"
              class="w-3 h-3 text-emerald-400"
            />
            <Copy
              v-else
              class="w-3 h-3"
            />
            <span>{{ copied === snippet.id ? '已复制' : '复制' }}</span>
          </button>
        </div>

        <pre class="mt-3 text-xs font-mono overflow-x-auto text-slate-200 leading-relaxed"><code>{{ snippet.code }}</code></pre>
      </div>
    </div>
  </div>
</template>
