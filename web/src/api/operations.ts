import { request } from './client.ts'
import type { LogEntry } from '@/types'

export interface Attempt {
  account_id?: string
  proxy?: string
  status_code: number
  elapsed_ms: number
  error?: string
}

export interface RequestRecord {
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
  elapsed_ms: number
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

export interface ProxyStatus {
  global_proxy: string
  signer: string
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
    return request<{ note: string }>('/api/proxies/global', { method: 'PUT', body: JSON.stringify({ proxy_url }) })
  },
  testProxy(options: { account_id?: string; proxy_url?: string } = {}) {
    return request<{ proxy: string; checks: ProxyCheck[] }>('/api/proxies/test', { method: 'POST', body: JSON.stringify(options) })
  },
}
