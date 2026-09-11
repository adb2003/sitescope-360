/**
 * Token storage, with a web fallback.
 *
 * On a device, tokens live in `expo-secure-store` — the OS keychain. A site tablet is shared,
 * left in a site office, and lost; the keychain is the only store that respects that.
 *
 * `expo-secure-store` has no web implementation, because a browser has no keychain. Expo web
 * exists here **to inspect the interface on a desktop**, not as a deployment target, so the web
 * path uses `localStorage` and says so rather than pretending to be secure. Anything reachable
 * from JavaScript in a browser is readable by anything else running on that origin.
 *
 * If this ever becomes a real browser client, this file is the wrong answer: that build should
 * hold tokens in memory with an httpOnly refresh cookie, which is what the web frontend in
 * `frontend/` already does.
 */

import { Platform } from 'react-native'

const ACCESS_KEY = 'sitescope.access'
const REFRESH_KEY = 'sitescope.refresh'

interface Store {
  getItemAsync(key: string): Promise<string | null>
  setItemAsync(key: string, value: string): Promise<void>
  deleteItemAsync(key: string): Promise<void>
}

/** Browser store. Not secure, and only ever reached on web — see the module docstring. */
const webStore: Store = {
  async getItemAsync(key) {
    try {
      return globalThis.localStorage?.getItem(key) ?? null
    } catch {
      // Private-mode browsers throw on access rather than returning null.
      return null
    }
  },
  async setItemAsync(key, value) {
    try {
      globalThis.localStorage?.setItem(key, value)
    } catch {
      // Nothing to do: a session that cannot persist still works until reload.
    }
  },
  async deleteItemAsync(key) {
    try {
      globalThis.localStorage?.removeItem(key)
    } catch {
      // Same.
    }
  },
}

function nativeStore(): Store {
  // Required lazily so the web bundle never pulls in a module with no web implementation.
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  return require('expo-secure-store') as Store
}

const store: Store = Platform.OS === 'web' ? webStore : nativeStore()

/** True when tokens are held somewhere the OS protects. Surfaced in the UI on web. */
export const TOKENS_ARE_SECURE = Platform.OS !== 'web'

export const tokens = {
  async get(): Promise<{ access: string | null; refresh: string | null }> {
    const [access, refresh] = await Promise.all([
      store.getItemAsync(ACCESS_KEY),
      store.getItemAsync(REFRESH_KEY),
    ])
    return { access, refresh }
  },
  async set(access: string, refresh: string): Promise<void> {
    await Promise.all([
      store.setItemAsync(ACCESS_KEY, access),
      store.setItemAsync(REFRESH_KEY, refresh),
    ])
  },
  async clear(): Promise<void> {
    await Promise.all([
      store.deleteItemAsync(ACCESS_KEY),
      store.deleteItemAsync(REFRESH_KEY),
    ])
  },
}
