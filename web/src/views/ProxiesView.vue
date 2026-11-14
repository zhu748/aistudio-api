<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Network, RefreshCw, Save, Users } from 'lucide-vue-next'
import { operationsApi, type ProxyStatus, type ProxyCheck, type ProxyAccount } from '@/api/operations.ts'
import { useToastStore } from '@/stores/toast.ts'
import Button from '@/components/ui/Button.vue'
import ProxyInputField from '@/components/ui/ProxyInputField.vue'
import { usePageRefresh } from '@/composables/usePageRefresh.ts'

const toast = useToastStore()
const status = ref<ProxyStatus>({ global_proxy: '', signer: '', accounts: [] })
const proxy = ref('')
const loading = ref(false)
const saving = ref(false)
const testing = ref('')
const checks = ref<ProxyCheck[]>([])
const checkedProxy = ref('')
const error = ref('')

/** 出口分组：同一有效出口（含「直连」）聚合为一行，避免多账号共用
 * 一条代理时表格里出现大量重复行；enabledCount 供徽标展示，
 * probeAccountId 取组内首个启用账号用于实际路由诊断。 */
interface ExitGroup {
  key: string
  proxy: string
  sources: Set<string>
  accounts: ProxyAccount[]
  enabledCount: number
  probeAccountId: string
}

const exitGroups = computed<ExitGroup[]>(() => {
  const groups = new Map<string, ExitGroup>()
  for (const account of status.value.accounts) {
    const key = account.proxy || '__direct__'
    let group = groups.get(key)
    if (!group) {
      group = {
        key,
        proxy: account.proxy,
        sources: new Set<string>(),
        accounts: [],
        enabledCount: 0,
        probeAccountId: '',
      }
      groups.set(key, group)
    }
    group.sources.add(account.source)
    group.accounts.push(account)
    if (account.enabled) {
      group.enabledCount += 1
      if (!group.probeAccountId) group.probeAccountId = account.account_id
    }
  }
  return [...groups.values()]
})

const totalAccounts = computed(() => status.value.accounts.length)

function sourceLabel(source: string) {
  return source === 'account' ? '账号独立' : source === 'global' ? '继承全局' : '直连'
}

function groupSourceLabel(group: ExitGroup) {
  return [...group.sources].map(sourceLabel).join(' + ')
}

async function load() {
  loading.value = true
  try {
    status.value = await operationsApi.proxies()
    error.value = ''
    return true
  } catch (err) {
    error.value = err instanceof Error ? err.message : '加载失败'
    return false
  } finally {
    loading.value = false
  }
}

async function save() {
  saving.value = true
  try {
    const result = await operationsApi.saveProxy(proxy.value.trim())
    toast.success(result.note)
    proxy.value = ''
    await load()
  } catch (err) {
    toast.error(err instanceof Error ? err.message : '保存失败')
  } finally {
    saving.value = false
  }
}

async function test(accountId = '', draft = false) {
  testing.value = draft ? 'draft' : accountId || 'global'
  checks.value = []
  try {
    const result = await operationsApi.testProxy(
      accountId ? { account_id: accountId } : draft ? { proxy_url: proxy.value.trim() } : {},
    )
    checks.value = result.checks
    checkedProxy.value = result.effective_proxy || result.proxy || '直连'
  } catch (err) {
    toast.error(err instanceof Error ? err.message : '诊断失败')
  } finally {
    testing.value = ''
  }
}

/** 按出口组诊断：优先用组内启用账号测真实路由；组内全是停用账号时
 * 回退为按代理地址测试（这些账号不会被调度，仅验证代理本身）。 */
async function testGroup(group: ExitGroup) {
  if (group.probeAccountId) {
    await test(group.probeAccountId)
  } else {
    testing.value = group.key
    checks.value = []
    try {
      const result = await operationsApi.testProxy(
        group.proxy ? { proxy_url: group.proxy } : {},
      )
      checks.value = result.checks
      checkedProxy.value = group.proxy || '直连'
    } catch (err) {
      toast.error(err instanceof Error ? err.message : '诊断失败')
    } finally {
      testing.value = ''
    }
  }
}
onMounted(load)
usePageRefresh(load)
</script>

<template>
  <div class="space-y-4">
    <div class="bg-white rounded-xl border border-slate-200 p-5 space-y-4">
      <div class="flex justify-between gap-3">
        <div>
          <h2 class="font-semibold flex gap-2 items-center">
            <Network class="w-4 h-4 text-brand-600" />全局代理
          </h2>
          <p class="text-xs text-slate-500 mt-1">
            当前出口：<code>{{ status.global_proxy || '直连' }}</code>
          </p>
        </div>
        <Button
          :loading="loading"
          @click="load"
        >
          <RefreshCw class="w-3.5 h-3.5" />刷新
        </Button>
      </div>
      <form
        class="space-y-2"
        @submit.prevent="save"
      >
        <div class="flex flex-wrap gap-2 items-start">
          <div class="flex-1 min-w-64">
            <ProxyInputField
              v-model="proxy"
              placeholder="输入完整新地址；留空保存为直连。粘贴自动识别类型"
            />
          </div>
          <Button
            :disabled="!!testing"
            @click="test('', true)"
          >
            测试输入
          </Button>
          <Button
            type="submit"
            variant="primary"
            :loading="saving"
          >
            <Save class="w-3.5 h-3.5" />保存
          </Button>
        </div>
      </form>
      <p class="text-xs text-slate-500">
        支持 HTTP、HTTPS、SOCKS5 与 TURN / TURNS / SSTP（后三者由桥接器转换为本地 SOCKS5，修改立即生效，账号独立代理优先；重启时环境变量配置优先）。
      </p>
      <div class="text-xs bg-slate-50 rounded-lg p-3 flex flex-wrap justify-between gap-2">
        <span class="flex flex-wrap gap-4">
          <span>签名节点：<code>{{ status.signer }}</code></span>
          <span>
            TURN/SSTP 桥接器：
            <span
              v-if="status.bridge?.available"
              class="text-emerald-600"
            >
              可用<template v-if="status.bridge.instances.length">（{{ status.bridge.instances.length }} 个运行中）</template>
            </span>
            <span
              v-else
              class="text-amber-600"
            >未安装 aistudio-proxy-bridge（使用 turn/sstp 前需构建或改用 Docker 镜像）</span>
          </span>
        </span>
        <Button
          size="xs"
          :loading="testing === 'global'"
          :disabled="!!testing"
          @click="test()"
        >
          诊断当前全局出口
        </Button>
      </div>
    </div>
    <div
      v-if="error"
      role="alert"
      class="bg-rose-50 text-rose-700 p-3 rounded-lg text-sm"
    >
      {{ error }}
    </div>
    <div
      v-if="testing || checks.length"
      class="bg-white border border-slate-200 rounded-xl p-5 space-y-3"
    >
      <h3 class="text-sm font-semibold">
        {{ testing ? '正在检测 Google 连通性与出口 IP…' : `诊断结果 · ${checkedProxy}` }}
      </h3>
      <p class="text-xs text-slate-500">
        测试不携带账号 Cookie。网络连通性通过后，仍需有效账号和可用签名服务才能完成模型调用。
      </p>
      <div class="grid grid-cols-1 sm:grid-cols-3 gap-3">
        <div
          v-for="check in checks"
          :key="check.name"
          class="rounded-lg border p-3 text-xs space-y-2"
          :class="check.ok ? 'border-emerald-200 bg-emerald-50' : 'border-rose-200 bg-rose-50'"
        >
          <p class="font-semibold">
            {{ check.name }} · {{ check.ok ? '通过' : '失败' }}
          </p>
          <p>
            {{ check.elapsed_ms }}ms <span v-if="check.status_code">· HTTP {{ check.status_code }}</span>
          </p>
          <p
            v-if="check.ip"
            class="font-mono"
          >
            {{ check.ip }}
          </p>
          <p
            v-if="check.error"
            class="break-all text-rose-700"
          >
            {{ check.error }}
          </p>
        </div>
      </div>
    </div>
    <div class="bg-white border border-slate-200 rounded-xl overflow-hidden">
      <div class="p-4 border-b border-slate-100 flex justify-between text-sm font-semibold">
        <span>账号出口路由 <span class="text-xs font-normal text-slate-400">按出口聚合</span></span><router-link
          to="/accounts"
          class="text-brand-600 text-xs"
        >
          编辑账号代理 →
        </router-link>
      </div>
      <div class="overflow-x-auto">
        <table class="w-full text-xs text-left">
          <thead class="bg-slate-50 text-slate-500">
            <tr>
              <th class="p-3">
                有效出口
              </th>
              <th class="p-3">
                来源
              </th>
              <th class="p-3">
                共用账号
              </th>
              <th class="p-3">
                诊断
              </th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-100">
            <tr
              v-for="group in exitGroups"
              :key="group.key"
            >
              <td class="p-3 font-mono break-all max-w-md">
                {{ group.proxy || '直连' }}
              </td>
              <td class="p-3">
                {{ groupSourceLabel(group) }}
              </td>
              <td class="p-3">
                <span
                  class="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 font-medium tabular-nums"
                  :class="group.accounts.length > 1 ? 'bg-brand-50 text-brand-700' : 'bg-slate-100 text-slate-600'"
                  :title="group.accounts.map(a => `${a.name}（${a.enabled ? '启用' : '停用'}）`).join('、')"
                >
                  <Users class="w-3 h-3 opacity-70" />
                  {{ group.enabledCount }}/{{ group.accounts.length }}
                </span>
              </td>
              <td class="p-3">
                <Button
                  size="xs"
                  :loading="testing === group.key"
                  :disabled="!!testing && testing !== group.key"
                  @click="testGroup(group)"
                >
                  测试出口
                </Button>
              </td>
            </tr>
            <tr v-if="!status.accounts.length">
              <td
                colspan="4"
                class="text-center text-slate-400 p-10"
              >
                导入账号后，可在这里查看每个账号的有效出口。
              </td>
            </tr>
            <tr v-else-if="!exitGroups.length">
              <td
                colspan="4"
                class="text-center text-slate-400 p-10"
              >
                暂无账号出口记录。
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p
        v-if="totalAccounts"
        class="px-4 py-2.5 border-t border-slate-100 text-[11px] text-slate-400"
      >
        共 {{ totalAccounts }} 个账号聚合为 {{ exitGroups.length }} 个出口；「x/y」表示该出口下启用/总账号数。同一出口测试一次即覆盖全部共用账号。
      </p>
    </div>
  </div>
</template>
