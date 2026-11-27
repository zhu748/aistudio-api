import { authHeaders, request } from './client.ts'
import type { LogEntry } from '@/types'
import type { RequestMetricsResponse } from '@/types/stats.ts'

export interface Attempt {
  error_code?: number
  account_id?: string
  proxy?: string
  status_code: number
  elapsed_ms: number
  error?: string
}

export interface ClientRequestCapture {
  source: 'client'
  method: string
  target: string
  http_version: string
  headers: [string, string][]
  body: string | null
  body_encoding: 'utf-8' | 'base64'
  body_bytes: number
  complete: boolean
  truncated: boolean
}

export interface RequestPayload {
  available: boolean
  note: string
  payload?: {
    client_request?: ClientRequestCapture
    body?: unknown
    response_body?: unknown
    request_body_truncated?: boolean
    response_body_truncated?: boolean
  }
}

export interface ThoughtSignatureItem {
  carrier: string
  kind: 'real' | 'skip_sentinel' | 'invalid' | 'missing'
  len: number
  fp: string
  head: string
  source?: string
  call_id?: string
  call_name?: string
  turn_index?: number
  note?: string
}

export interface ThoughtSignatureReplayItem {
  turn_index: number
  action: 'warm_hit' | 'cold_hit' | 'skip_fill' | 'conflict' | 'miss'
  fc_count: number
  restored_sigs: number
  reason?: string
  continuation_reused?: boolean
}

export interface ThoughtSignatureTrace {
  protocol: string
  attempt: number
  client_in: {
    model_turns: number
    fc_total: number
    fc_signed: number
    fc_skip: number
    fc_missing: number
    thought_total: number
    thought_signed: number
    tail_signed: number
    other_signed: number
    dropped: number
    sources: string[]
    items: ThoughtSignatureItem[]
  }
  replay: {
    warm_hits: number
    cold_hits: number
    skip_fills: number
    misses: number
    conflicts: number
    continuation_reused: boolean
    miss_reasons: string[]
    items: ThoughtSignatureReplayItem[]
  }
  upstream_out: {
    real_sigs: number
    skip_sigs: number
    fc_total: number
    fc_unsigned: number
    continuation_sent: boolean
    fingerprints: string[]
    items: ThoughtSignatureItem[]
  }
  upstream_in: {
    total_sigs: number
    by_carrier: Record<string, number>
    cached_warm: boolean
    cached_cold: boolean
    cache_status: string
    cache_note: string
    fingerprints: string[]
    items: ThoughtSignatureItem[]
  }
  client_out: {
    total_sigs: number
    carriers: string[]
    dropped: number
    drop_reasons: string[]
    fingerprints: string[]
    items: ThoughtSignatureItem[]
  }
}

export interface RequestRecord {
  request_parameters?: Record<string, unknown>
  effective_parameters?: Record<string, unknown>
  first_content_ms?: number | null
  first_thought_ms?: number | null
  generation_ms?: number | null
  finished_at?: number
  protocol?: string
  requested_model?: string
  account_name?: string
  stage?: string
  events?: { stage: string; message: string; elapsed_ms: number; account_id?: string }[]
  thought_signature?: ThoughtSignatureTrace | null
  request_id: string
  started_at: number
  method: string
  path: string
  model: string
  account_id: string
  key_id: string
  proxy: string
  signer: string
  result: string
  error: string
  finish_reason: string
  status_code: number
  is_stream: boolean
  elapsed_ms: number | null
  first_byte_ms: number | null
  usage: Record<string, unknown>
  attempts: Attempt[]
}

export interface Page<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}

export interface ProxyAccount {
  account_id: string
  name: string
  enabled: boolean
  source: string
  proxy: string
}

export interface BridgeInstance {
  port: number
  uri: string
  alive: boolean
  started_at: number
}

export interface ProxyStatus {
  global_proxy: string
  signer: string
  bridge?: {
    available: boolean
    binary: string
    idle_minutes: number
    instances: BridgeInstance[]
  }
  accounts: ProxyAccount[]
}

export interface ProxyCheck {
  name: string
  ok: boolean
  elapsed_ms: number
  status_code?: number
  ip?: string
  error?: string
}

export const operationsApi = {
  metrics(params: URLSearchParams) {
    return request<RequestMetricsResponse>(`/api/requests/stats?${params}`)
  },
  active() {
    return request<{ items: RequestRecord[]; total: number }>('/api/requests/active')
  },
  options() {
    return request<{
      models: string[]
      keys: string[]
      key_names: Record<string, string>
      accounts: { id: string; name: string }[]
      history_days: number
      enabled: boolean
    }>('/api/requests/options')
  },
  detail(id: string) {
    return request<RequestRecord>(`/api/requests/${encodeURIComponent(id)}`)
  },
  payload(id: string) {
    return request<RequestPayload>(
      `/api/requests/${encodeURIComponent(id)}/payload`,
    )
  },
  upstream(id: string) {
    return request<{
      available?: boolean
      note?: string
      captured_at?: string
      upstream?: {
        url: string
        method: string
        headers: Record<string, string>
        wire_body: unknown
        body_bytes: number
      }
      proxy?: string | null
      response?: { status: number; body: string }
    }>(`/api/requests/${encodeURIComponent(id)}/upstream`)
  },
  async upstreamBlob(id: string) {
    // 超时兑底：后端挂起时 fetch 永不 settle，调用方的下载 loading 态会
    // 永久转圈；AbortController 60s 超时与常规请求超时口径一致。
    const controller = new AbortController()
    const timeout = setTimeout(() => controller.abort(), 60_000)
    try {
      const response = await fetch(
        `/api/requests/${encodeURIComponent(id)}/upstream?download=1`,
        { headers: await authHeaders(), signal: controller.signal },
      )
      if (!response.ok) throw new Error(`下载失败 (HTTP ${response.status})`)
      return await response.blob()
    } catch (err: unknown) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        // ES2020 lib 无 Error cause 形参，用 Object.assign 携带（满足
        // preserve-caught-error：症状错误保留原因链）
        throw Object.assign(new Error('下载超时（60 秒），请稍后重试'), { cause: err })
      }
      throw err
    } finally {
      clearTimeout(timeout)
    }
  },
  upstreamSnapshotStats() {
    return request<{
      success: number
      error: number
      bytes: number
      keep: number
    }>('/api/requests/upstream-snapshots')
  },
  clearUpstreamSnapshots(scope: 'all' | 'success') {
    return request<{
      ok: boolean
      scope: string
      removed: { success: number; error: number }
    }>(`/api/requests/upstream-snapshots?scope=${scope}`, { method: 'DELETE' })
  },
  requests(params: URLSearchParams) {
    return request<Page<RequestRecord>>(`/api/requests?${params}`)
  },
  exportRequests(params: URLSearchParams) {
    return request<Page<RequestRecord>>(`/api/requests/export?${params}`)
  },
  clearRequests() {
    return request<{ removed: number }>('/api/requests', { method: 'DELETE' })
  },
  logs(params: URLSearchParams) {
    return request<Page<LogEntry>>(`/api/logs/history?${params}`)
  },
  proxies() {
    return request<ProxyStatus>('/api/proxies')
  },
  saveProxy(proxy_url: string) {
    return request<{ note: string }>('/api/proxies/global', {
      method: 'PUT',
      body: JSON.stringify({ proxy_url }),
    })
  },
  testProxy(options: { account_id?: string; proxy_url?: string } = {}) {
    return request<{ proxy: string; effective_proxy?: string; checks: ProxyCheck[] }>(
      '/api/proxies/test',
      {
        method: 'POST',
        body: JSON.stringify(options),
      },
    )
  },
}
