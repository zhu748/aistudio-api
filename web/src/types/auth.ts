export interface AuthCheckResponse {
  auth_enabled: boolean
}

/** POST /auth/session 响应：持久会话令牌（expires_at 为 Unix 秒，null 表示不过期）。 */
export interface SessionResponse {
  ok: boolean
  token: string
  expires_at: number | null
}
