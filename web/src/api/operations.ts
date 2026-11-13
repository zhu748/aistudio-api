import { request } from './client.ts'
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
    return request<{ available: boolean; note: string; payload?: Record<string, unknown> }>(
      `/api/requests/${encodeURIComponent(id)}/payload`,
    )
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
