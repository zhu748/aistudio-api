export interface KeepaliveConfig {
  /** 表单值：运行时覆盖生效中为覆盖值，否则 env 注入字段预填 env 值、其余为 yaml 值 */
  url: string
  interval: number
  allow_private: boolean
  /** 保活任务当前是否在运行（运行态） */
  running: boolean
  /** 环境变量注入标注（key → env 名；面板保存可覆盖，重启后回退） */
  env_overrides?: Record<string, string>
  /** 面板保存的运行时覆盖是否生效中 */
  runtime_override_active?: boolean
  /** config.yaml 落盘值（供前端展示「保存后将被面板值覆盖」） */
  yaml_url?: string
  yaml_interval?: number
  yaml_allow_private?: boolean
}

export interface UpdateKeepaliveConfigRequest {
  url: string
  interval: number
  allow_private: boolean
}
