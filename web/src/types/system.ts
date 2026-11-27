export interface SystemConfig {
  port: number
  proof_server_url?: string
  proxy_configured: boolean
  auth_enabled: boolean
  yaml_content: string
  log_level?: string
  dump_requests?: boolean
  debug_env_active?: boolean
  dump_to_file?: boolean
  dump_dir?: string
  upstream_snapshot_keep?: number
}
export interface UpdateLoggingConfigRequest {
  level?: string
  dump_requests?: boolean
  dump_to_file?: boolean
  dump_dir?: string
  upstream_snapshot_keep?: number
}
export interface UpdateLoggingConfigResponse {
  ok: boolean
  log_level: string
  dump_requests: boolean
  debug_env_active: boolean
  dump_to_file: boolean
  dump_dir: string
  upstream_snapshot_keep: number
}

export interface HealthCheckResponse {
  status: string
}
export interface ApiKeyItem {
  name: string
  key: string
  created_at?: string | null
}

export interface CreateApiKeyRequest {
  name?: string
  key?: string
}

export interface UpdateApiKeyRequest {
  name: string
}

export interface AvailableModelItem {
  id: string
  displayName: string
}

export interface ThoughtSignatureLogMeta {
  direction: 'outbound' | 'inbound'
  protocol: string
  status: 'ok' | 'fallback' | 'warn' | 'partial' | 'dropped'
  attempt?: number
  // Outbound fields
  client_model_turns?: number
  client_fc_total?: number
  client_fc_signed?: number
  client_fc_missing?: number
  client_thought_signed?: number
  client_tail_signed?: number
  client_dropped?: number
  client_sources?: string[]
  warm_hits?: number
  cold_hits?: number
  skip_fills?: number
  conflicts?: number
  misses?: number
  continuation_reused?: boolean
  upstream_real?: number
  upstream_skip?: number
  upstream_fc_unsigned?: number
  /** Model turns whose first functionCall went upstream bare (the real 400 risk). */
  upstream_fc_unsigned_lead?: number
  // Inbound fields
  upstream_total?: number
  by_carrier?: Record<string, number>
  cached_warm?: boolean
  cached_cold?: boolean
  cache_status?: string
  cache_note?: string
  client_total?: number
  client_carriers?: string[]
  drop_reasons?: string[]
  fingerprints?: string[]
}

export interface LogEntry {
  category?: 'model' | 'system'
  model?: string
  account_id?: string
  stage?: string
  request_id?: string
  sig_meta?: ThoughtSignatureLogMeta
  ts: string
  ts_ms: number
  level: string
  name: string
  message: string
}

export interface LogsResponse {
  ok: boolean
  count: number
  total: number
  capacity: number
  logs: LogEntry[]
}

export interface AntiTruncationConfig {
  enabled: boolean
  tool_name: string
  variants: {
    enabled: boolean
    models: string[]
  }
  available_models: AvailableModelItem[]
}

export interface UpdateAntiTruncationConfigRequest {
  enabled?: boolean
  tool_name?: string
  variants_enabled?: boolean
  models?: string[]
}
