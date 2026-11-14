export class ApiError extends Error {
  status: number
  detail: string

  constructor(status: number, detail: string) {
    super(detail)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

const TOKEN_KEY = 'asp_api_token'

// 令牌存储（3.17）：localStorage 使管理密码在浏览器中永久留存（任何 XSS
// 都可直接窃取、无过期机制、shared device 上换用户也残留）。改为
// sessionStorage + 进程内缓存：令牌随标签页会话结束自动消失，浏览器
// 重启后需重新登录；首次读取时顺带把历史 localStorage 令牌迁移清理。
let cachedToken: string | null = null

export function getToken(): string {
  if (cachedToken !== null) return cachedToken
  // 历史版本把令牌落在 localStorage：迁移到 sessionStorage 并清除原值
  const legacy = localStorage.getItem(TOKEN_KEY)
  if (legacy) {
    localStorage.removeItem(TOKEN_KEY)
    try {
      sessionStorage.setItem(TOKEN_KEY, legacy)
    } catch {
      /* sessionStorage 不可用（隐私模式等）时退化为仅内存缓存 */
    }
    cachedToken = legacy
    return legacy
  }
  try {
    cachedToken = sessionStorage.getItem(TOKEN_KEY) || ''
  } catch {
    cachedToken = ''
  }
  return cachedToken
}

export function setToken(token: string): void {
  const trimmed = token.trim()
  cachedToken = trimmed
  try {
    if (trimmed) {
      sessionStorage.setItem(TOKEN_KEY, trimmed)
    } else {
      sessionStorage.removeItem(TOKEN_KEY)
    }
  } catch {
    /* 存储不可用时仅保留内存缓存 */
  }
  localStorage.removeItem(TOKEN_KEY)
}

export function clearToken(): void {
  cachedToken = ''
  try {
    sessionStorage.removeItem(TOKEN_KEY)
  } catch {
    /* ignore */
  }
  localStorage.removeItem(TOKEN_KEY)
}

type UnauthorizedHandler = () => void
let onUnauthorizedHandler: UnauthorizedHandler | null = null

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorizedHandler = handler
}

/** 构造带管理令牌的请求头（供 request 之外的裸 fetch 使用，如 blob 下载）。 */
export async function authHeaders(): Promise<Record<string, string>> {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

// 默认请求超时：fetch 本身没有超时，后端卡住时前端会无限等待
const DEFAULT_TIMEOUT_MS = 30000

/** request 额外支持的选项（不属于 RequestInit 的部分）。 */
export interface RequestOptions extends RequestInit {
  /** 覆盖默认超时（毫秒）。 */
  timeoutMs?: number
  acceptableStatuses?: number[]
}

export async function request<T>(url: string, options: RequestOptions = {}): Promise<T> {
  const { timeoutMs, acceptableStatuses = [], ...init } = options
  const headers = new Headers(init.headers || {})
  const token = getToken()

  if (token && !headers.has('Authorization')) {
    headers.set('Authorization', `Bearer ${token}`)
  }

  if (!headers.has('Content-Type') && !(init.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json')
  }

  if (!headers.has('Accept')) {
    headers.set('Accept', 'application/json')
  }

  // 调用方已自带 signal 时不覆盖，否则用 AbortController 施加默认超时
  let timeoutId: ReturnType<typeof setTimeout> | undefined
  let signal = init.signal
  if (!signal && typeof AbortController !== 'undefined') {
    const controller = new AbortController()
    timeoutId = setTimeout(() => controller.abort(), timeoutMs ?? DEFAULT_TIMEOUT_MS)
    signal = controller.signal
  }

  let response: Response
  let text: string
  try {
    try {
      response = await fetch(url, {
        ...init,
        headers,
        signal,
      })
    } catch (err) {
      if (err instanceof DOMException && err.name === 'AbortError') {
        throw new ApiError(0, '请求超时，请稍后重试')
      }
      throw err
    }

    if (!response.ok && !acceptableStatuses.includes(response.status)) {
      if (response.status === 401) {
        clearToken()
        if (onUnauthorizedHandler) {
          onUnauthorizedHandler()
        } else if (typeof window !== 'undefined' && !window.location.pathname.startsWith('/login')) {
          const redirect = encodeURIComponent(window.location.pathname + window.location.search)
          window.location.href = `/login?redirect=${redirect}`
        }
      }

      let errorDetail = `请求失败 (${response.status})`
      try {
        const errJson = await response.json()
        if (errJson && typeof errJson.detail === 'string') {
          errorDetail = errJson.detail
        } else if (errJson && typeof errJson.detail?.message === 'string') {
          // FastAPI 的 detail 可能是对象（本项目大量抛结构化 detail）
          errorDetail = errJson.detail.message
        } else if (errJson && typeof errJson.error?.message === 'string') {
          errorDetail = errJson.error.message
        }
      } catch {
        // ignore json parse error
      }
      throw new ApiError(response.status, errorDetail)
    }

    // 读取响应体也必须在超时窗口内：否则慢速滴流的响应体可以无限挂起
    text = await response.text()
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId)
  }

  // 204 No Content
  if (response.status === 204) {
    return {} as T
  }

  // 200 但响应体为空时，直接 JSON.parse 会抛 SyntaxError，这里按空对象处理
  if (!text.trim()) {
    return {} as T
  }
  return JSON.parse(text) as T
}
