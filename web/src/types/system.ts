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
  auth_token_env?: string
  id: string
  name: string
  description?: string
  url: string
  user_agent?: string
  headers?: Record<string, string>
  is_active?: boolean
  /** 该节点是否为环境变量 AISTUDIO_WAA_SERVER 指定的启动默认（重启后恢复） */
  is_env_default?: boolean
  /** 兼容旧字段，语义同 is_env_default */
  env_locked?: boolean
}

export interface SignerServersResponse {
  active_server: string
  env_override?: string | null
  /** 面板切换写入的运行时覆盖 id（未设置时为 null） */
  runtime_override?: string | null
  servers: SignerServerItem[]
}

export interface SwitchSignerServerResponse {
  ok: boolean
  active_server: SignerServerItem
  runtime_override?: string | null
  /** 信息提示：环境变量默认与切换目标不一致时说明重启后的回退行为 */
  note?: string | null
}

export interface ResetSignerServerResponse {
  ok: boolean
  active_server: SignerServerItem
  runtime_override?: string | null
  /** 是否真正清除了运行时覆盖（原本未设置时为 false） */
  changed?: boolean
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
