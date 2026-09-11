/**
 * API client for the mobile app.
 *
 * Deliberately small and separate from the web client: the mobile app has different constraints
 * (SecureStore rather than localStorage, React Native's FormData file shape, and no CORS), and
 * sharing one client would mean abstracting over both platforms for very little code.
 *
 * Tokens live in `expo-secure-store` — the OS keychain — not AsyncStorage. A site tablet is a
 * shared, easily-lost device, and a bearer token in plaintext application storage is a real risk.
 */

import Constants from 'expo-constants'
import { Platform } from 'react-native'

// Imported for use below and re-exported so existing callers keep importing `tokens` from
// here; the platform split lives in tokenStore.ts because a browser has no keychain.
import { tokens } from './tokenStore'

export { tokens }

const ACCESS_KEY = 'sitescope_access_token'
const REFRESH_KEY = 'sitescope_refresh_token'

/**
 * `10.0.2.2` is the Android emulator's alias for the host machine; a physical device needs the
 * host's LAN address. Configured in `app.json` under `extra.apiBaseUrl`, with
 * `extra.apiBaseUrlWeb` used under Expo web -- 10.0.2.2 is the Android emulator's alias for
 * the host and means nothing in a desktop browser.
 */
export const API_BASE_URL: string =
  Platform.OS === 'web'
    ? ((Constants.expoConfig?.extra?.apiBaseUrlWeb as string | undefined) ??
      'http://localhost:8000')
    : ((Constants.expoConfig?.extra?.apiBaseUrl as string | undefined) ?? 'http://10.0.2.2:8000')

const API_PREFIX = '/api'

export class ApiError extends Error {
  readonly status: number
  readonly code: string

  constructor(status: number, code: string, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

async function authHeaders(): Promise<Record<string, string>> {
  const { access } = await tokens.get()
  return access ? { Authorization: `Bearer ${access}` } : {}
}

async function handle<T>(response: Response): Promise<T> {
  if (!response.ok) {
    let code = `http_${response.status}`
    let message = response.statusText || 'Request failed'
    try {
      const body = (await response.json()) as { code?: string; message?: string }
      code = body.code ?? code
      message = body.message ?? message
    } catch {
      // Non-JSON error body; the status text above is the best available message.
    }
    throw new ApiError(response.status, code, message)
  }
  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

/** Refresh once on a 401, then retry. Mirrors the web client's behaviour. */
async function withRefresh<T>(send: () => Promise<Response>): Promise<T> {
  let response = await send()
  if (response.status === 401) {
    const { refresh } = await tokens.get()
    if (refresh) {
      const refreshed = await fetch(`${API_BASE_URL}${API_PREFIX}/auth/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refresh }),
      })
      if (refreshed.ok) {
        const pair = (await refreshed.json()) as { access_token: string; refresh_token: string }
        await tokens.set(pair.access_token, pair.refresh_token)
        response = await send()
      } else {
        await tokens.clear()
      }
    }
  }
  return handle<T>(response)
}

export const api = {
  async login(email: string, password: string): Promise<void> {
    const response = await fetch(`${API_BASE_URL}${API_PREFIX}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
    })
    const pair = await handle<{ access_token: string; refresh_token: string }>(response)
    await tokens.set(pair.access_token, pair.refresh_token)
  },

  async get<T>(path: string): Promise<T> {
    return withRefresh<T>(async () =>
      fetch(`${API_BASE_URL}${API_PREFIX}${path}`, { headers: await authHeaders() }),
    )
  },

  async post<T>(path: string, body: unknown): Promise<T> {
    return withRefresh<T>(async () =>
      fetch(`${API_BASE_URL}${API_PREFIX}${path}`, {
        method: 'POST',
        headers: { ...(await authHeaders()), 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      }),
    )
  },

  /**
   * Upload one file.
   *
   * React Native's FormData accepts a `{ uri, name, type }` object in place of a Blob and streams
   * from disk — which is the only way a 2 GiB video can be uploaded without loading it into JS
   * memory. The type assertion is unavoidable: the DOM FormData type does not describe it.
   */
  async uploadFile<T>(
    path: string,
    file: { uri: string; name: string; type: string },
  ): Promise<T> {
    const form = new FormData()
    form.append('files', file as unknown as Blob)
    return withRefresh<T>(async () =>
      fetch(`${API_BASE_URL}${API_PREFIX}${path}`, {
        method: 'POST',
        // No Content-Type: React Native sets the multipart boundary itself.
        headers: await authHeaders(),
        body: form,
      }),
    )
  },
}
