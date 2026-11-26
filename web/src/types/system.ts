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

export interface LogEntry {
  category?: 'model' | 'system'
  model?: string
  account_id?: string
  stage?: string
  request_id?: string
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
