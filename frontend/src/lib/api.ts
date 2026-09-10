/**
 * API client.
 *
 * Three responsibilities, kept in one place so no component ever touches `fetch`:
 *
 * 1. Attach the access token, and transparently refresh it once on a 401 — a site engineer
 *    uploading a video should not be logged out mid-session because a token aged out.
 * 2. Turn the backend's `{code, message, details}` envelope into a typed `ApiError`, so a form
 *    can show field-level messages instead of "something went wrong".
 * 3. Serialise query parameters consistently, including repeated values for list filters.
 */

import type { ErrorEnvelope } from '@/types/api'

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? ''
const API_PREFIX = '/api'

const ACCESS_TOKEN_KEY = 'sitescope.access_token'
const REFRESH_TOKEN_KEY = 'sitescope.refresh_token'

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly details?: Record<string, unknown>

  constructor(status: number, envelope: ErrorEnvelope) {
    super(envelope.message)
    this.name = 'ApiError'
    this.status = status
    this.code = envelope.code
    this.details = envelope.details
  }

  /** Field-level messages from a 422, keyed by field path, for inline form errors. */
  get fieldErrors(): Record<string, string> {
    const fields = this.details?.fields
    if (!Array.isArray(fields)) return {}
    const out: Record<string, string> = {}
    for (const entry of fields) {
      if (entry && typeof entry === 'object' && 'field' in entry && 'message' in entry) {
        out[String(entry.field)] = String(entry.message)
      }
    }
    return out
  }

  get isAuthError(): boolean {
    return this.status === 401
  }

  get isPermissionError(): boolean {
    return this.status === 403
  }
}

// ---- token storage ----------------------------------------------------------
export const tokens = {
  get access() {
    return safeGet(ACCESS_TOKEN_KEY)
  },
  get refresh() {
    return safeGet(REFRESH_TOKEN_KEY)
  },
  set(access: string, refresh: string) {
    safeSet(ACCESS_TOKEN_KEY, access)
    safeSet(REFRESH_TOKEN_KEY, refresh)
  },
  clear() {
    safeRemove(ACCESS_TOKEN_KEY)
    safeRemove(REFRESH_TOKEN_KEY)
  },
}

function safeGet(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    // Private windows and locked-down browsers throw on access. A session that lives only in
    // memory is degraded but working; crashing the app is not an option.
    return memoryStore.get(key) ?? null
  }
}

function safeSet(key: string, value: string) {
  try {
    localStorage.setItem(key, value)
  } catch {
    memoryStore.set(key, value)
  }
}

function safeRemove(key: string) {
  try {
    localStorage.removeItem(key)
  } catch {
    memoryStore.delete(key)
  }
}

const memoryStore = new Map<string, string>()

// ---- request ----------------------------------------------------------------
export type QueryValue = string | number | boolean | null | undefined | Array<string | number>

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'PUT' | 'DELETE'
  body?: unknown
  query?: Record<string, QueryValue>
  /** Multipart payload. Mutually exclusive with `body`. */
  form?: FormData
  signal?: AbortSignal
  /** Skip the Authorization header (login, refresh, health). */
  anonymous?: boolean
  /** Return the raw Response instead of parsed JSON (file downloads). */
  raw?: boolean
}

export function buildUrl(path: string, query?: Record<string, QueryValue>): string {
  const url = `${API_BASE}${API_PREFIX}${path}`
  if (!query) return url

  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value === null || value === undefined || value === '') continue
    if (Array.isArray(value)) {
      // Repeated keys, matching FastAPI's list query convention.
      for (const item of value) params.append(key, String(item))
    } else {
      params.append(key, String(value))
    }
  }
  const qs = params.toString()
  return qs ? `${url}?${qs}` : url
}

/** Set when a request fails authentication irrecoverably, so the app can route to /login. */
let onUnauthorized: (() => void) | null = null
export function setUnauthorizedHandler(handler: (() => void) | null) {
  onUnauthorized = handler
}

/**
 * A single in-flight refresh shared by every concurrent 401. The dashboard fires half a dozen
 * queries at once; without this they would each rotate the refresh token and all but one would
 * fail, because rotation deliberately invalidates the presented token.
 */
let refreshInFlight: Promise<boolean> | null = null

async function refreshAccessToken(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight

  refreshInFlight = (async () => {
    const refresh = tokens.refresh
    if (!refresh) return false
    try {
      const response = await fetch(buildUrl('/auth/refresh'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refresh }),
      })
      if (!response.ok) return false
      const data = (await response.json()) as { access_token: string; refresh_token: string }
      tokens.set(data.access_token, data.refresh_token)
      return true
    } catch {
      return false
    } finally {
      // Cleared synchronously when the refresh settles. Deferring this to a later tick would
      // leave a *resolved* promise in the slot, so a subsequent 401 would reuse the previous
      // outcome instead of attempting a real refresh — reporting success without refreshing.
      // Clearing here is still safe for deduplication: every caller that arrives while the
      // refresh is in flight has already captured this same promise.
      refreshInFlight = null
    }
  })()

  return refreshInFlight
}

export async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await send(path, options)

  if (response.status === 401 && !options.anonymous) {
    const refreshed = await refreshAccessToken()
    if (refreshed) {
      const retried = await send(path, options)
      return finish<T>(retried, options)
    }
    tokens.clear()
    onUnauthorized?.()
  }

  return finish<T>(response, options)
}

async function send(path: string, options: RequestOptions): Promise<Response> {
  const headers: Record<string, string> = {}
  if (!options.anonymous) {
    const token = tokens.access
    if (token) headers.Authorization = `Bearer ${token}`
  }

  let body: BodyInit | undefined
  if (options.form) {
    // Deliberately no Content-Type: the browser must set the multipart boundary itself.
    body = options.form
  } else if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(options.body)
  }

  return fetch(buildUrl(path, options.query), {
    method: options.method ?? 'GET',
    headers,
    body,
    signal: options.signal,
  })
}

async function finish<T>(response: Response, options: RequestOptions): Promise<T> {
  if (!response.ok) {
    throw new ApiError(response.status, await readError(response))
  }
  if (options.raw) return response as unknown as T
  if (response.status === 204) return undefined as T

  const contentType = response.headers.get('content-type') ?? ''
  if (!contentType.includes('application/json')) {
    return (await response.text()) as unknown as T
  }
  return (await response.json()) as T
}

async function readError(response: Response): Promise<ErrorEnvelope> {
  try {
    const data = (await response.json()) as Partial<ErrorEnvelope>
    if (data && typeof data.message === 'string') {
      return {
        code: data.code ?? `http_${response.status}`,
        message: data.message,
        details: data.details,
      }
    }
  } catch {
    // Fall through to a generic message below.
  }
  return {
    code: `http_${response.status}`,
    message: response.status === 0 ? 'Cannot reach the server' : response.statusText || 'Request failed',
  }
}

// ---- convenience verbs ------------------------------------------------------
export const api = {
  get: <T>(path: string, query?: Record<string, QueryValue>, signal?: AbortSignal) =>
    request<T>(path, { query, signal }),
  post: <T>(path: string, body?: unknown, query?: Record<string, QueryValue>) =>
    request<T>(path, { method: 'POST', body, query }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body }),
  put: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PUT', body }),
  delete: <T>(path: string, query?: Record<string, QueryValue>) =>
    request<T>(path, { method: 'DELETE', query }),
  upload: <T>(path: string, form: FormData) => request<T>(path, { method: 'POST', form }),
  raw: (path: string, query?: Record<string, QueryValue>) =>
    request<Response>(path, { query, raw: true }),
  anonymous: {
    post: <T>(path: string, body?: unknown) =>
      request<T>(path, { method: 'POST', body, anonymous: true }),
    get: <T>(path: string) => request<T>(path, { anonymous: true }),
  },
}
