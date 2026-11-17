export interface KeepaliveConfig {
  url: string
  interval: number
  allow_private: boolean
  /** 保活任务当前是否在运行（运行态） */
  running: boolean
  /** 环境变量覆盖提示（key → env 名） */
  env_overrides?: Record<string, string>
  /** 运行中生效值（环境变量 > yaml） */
  effective_url?: string
  effective_interval?: number
  effective_allow_private?: boolean
}

export interface UpdateKeepaliveConfigRequest {
  url: string
  interval: number
  allow_private: boolean
}
