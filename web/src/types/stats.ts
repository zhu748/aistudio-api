export interface AccountBreakdownItem {
  account_id: string
  name: string
  auth_user: string
  email: string | null
  tier: string
  group: string
  enabled: boolean
  today_requests: number
  today_success: number
  today_rate_limited: number
  today_tokens: number
  today_reasoning_tokens: number
  configured_quota: number
  quota_used_percent: number
  is_available: boolean
  cooldown_remaining: number
}

export interface ModelStatItem {
  accounts?: (ModelStatItem & { account_id: string; name: string })[]
  requests: number
  success: number
  rate_limited: number
  errors: number
  prompt_tokens: number
  completion_tokens: number
  reasoning_tokens?: number
  cached_tokens?: number
  total_tokens: number
  finish_reasons?: Record<string, number>
  last_used?: string | null
  account_breakdown?: AccountBreakdownItem[]
  attempts?: number
  retried?: number
  success_rate?: number | null
  retry_rate?: number | null
  latency_p50_ms?: number | null
  latency_p95_ms?: number | null
  first_byte_p50_ms?: number | null
  first_byte_p95_ms?: number | null
  results?: Record<string, number>
}

export interface StatsTotals {
  requests: number
  success: number
  rate_limited: number
  errors: number
  prompt_tokens: number
  completion_tokens: number
  reasoning_tokens?: number
  cached_tokens?: number
  total_tokens: number
}

export interface StatsResponse {
  models: Record<string, ModelStatItem>
  totals: StatsTotals
}

export interface RequestMetricsResponse {
  models: Record<string, ModelStatItem>
  totals: ModelStatItem
  accounts: Record<string, ModelStatItem & { name: string }>
  keys: Record<string, ModelStatItem>
  series: (ModelStatItem & { timestamp: number })[]
  since: number
  until: number
  interval_seconds: number
  history_days: number
  history_enabled: boolean
  active: number
}
