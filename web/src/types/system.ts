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
}
export interface UpdateLoggingConfigRequest {
  level?: string
  dump_requests?: boolean
  dump_to_file?: boolean
  dump_dir?: string
}
export interface UpdateLoggingConfigResponse {
  ok: boolean
  log_level: string
  dump_requests: boolean
  debug_env_active: boolean
  dump_to_file: boolean
  dump_dir: string
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

export interface SignerServerItem {
  id: string
  name: string
  description?: string
  url: string
  user_agent?: string
  headers?: Record<string, string>
  is_active?: boolean
  env_locked?: boolean
}

export interface SignerServersResponse {
  active_server: string
  env_override?: string | null
  servers: SignerServerItem[]
}

export interface TestSignerServerResponse {
  ok: boolean
  status_code?: number
  elapsed_ms: number
  error?: string
  data?: Record<string, unknown>
}

export interface AvailableModelItem {
  id: string
  displayName: string
}

export interface LogEntry {
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
