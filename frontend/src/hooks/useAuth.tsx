import { useQuery, useQueryClient } from '@tanstack/react-query'
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'

import { ApiError, api, setUnauthorizedHandler, tokens } from '@/lib/api'
import type { SessionInfo, TokenPair } from '@/types/api'

export interface AuthState {
  session: SessionInfo | null
  loading: boolean
  error: string | null
  signIn: (email: string, password: string) => Promise<void>
  signOut: () => Promise<void>
  /** Capability check driven by the server's own list — never by re-deriving the role table. */
  can: (capability: string) => boolean
}

const AuthContext = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [hasToken, setHasToken] = useState(() => Boolean(tokens.access))
  const [error, setError] = useState<string | null>(null)

  const { data, isLoading, isError } = useQuery({
    queryKey: ['session'],
    queryFn: () => api.get<SessionInfo>('/auth/me'),
    enabled: hasToken,
    retry: false,
    staleTime: 5 * 60 * 1000,
  })

  const signOut = useCallback(async () => {
    const refresh = tokens.refresh
    tokens.clear()
    setHasToken(false)
    // Clear cached data before the network call: the user is signed out from their point of
    // view immediately, and a failed logout request must not leave another account's data on
    // screen.
    queryClient.clear()
    if (refresh) {
      try {
        await api.post('/auth/logout', { refresh_token: refresh })
      } catch {
        // The local session is already gone; a failed revoke is not worth surfacing.
      }
    }
  }, [queryClient])

  const signIn = useCallback(
    async (email: string, password: string) => {
      setError(null)
      try {
        const pair = await api.anonymous.post<TokenPair>('/auth/login', { email, password })
        tokens.set(pair.access_token, pair.refresh_token)
        setHasToken(true)
        await queryClient.invalidateQueries({ queryKey: ['session'] })
      } catch (caught) {
        const message =
          caught instanceof ApiError ? caught.message : 'Cannot reach the server. Is the API running?'
        setError(message)
        throw caught
      }
    },
    [queryClient],
  )

  // An unrecoverable 401 from any request drops the session, so an expired refresh token routes
  // to the sign-in screen instead of leaving a shell of empty panels.
  useEffect(() => {
    setUnauthorizedHandler(() => {
      setHasToken(false)
      queryClient.clear()
    })
    return () => setUnauthorizedHandler(null)
  }, [queryClient])

  const capabilities = useMemo(() => new Set(data?.capabilities ?? []), [data])

  const value = useMemo<AuthState>(
    () => ({
      session: hasToken && !isError ? (data ?? null) : null,
      loading: hasToken && isLoading,
      error,
      signIn,
      signOut,
      can: (capability: string) => capabilities.has(capability),
    }),
    [hasToken, isError, data, isLoading, error, signIn, signOut, capabilities],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used inside <AuthProvider>')
  return context
}

/** Capabilities used by the UI, matching `app/auth/rbac.py`. */
export const Capability = {
  ProjectRead: 'project:read',
  ProjectWrite: 'project:write',
  ProjectDelete: 'project:delete',
  CaptureRead: 'capture:read',
  CaptureCreate: 'capture:create',
  CaptureDelete: 'capture:delete',
  Upload: 'upload',
  Annotate: 'annotate',
  AnnotationResolve: 'annotation:resolve',
  FloorPlanEdit: 'floorplan:edit',
  LocationEdit: 'location:edit',
  ReportGenerate: 'report:generate',
  SettingsProject: 'settings:project',
  SettingsSystem: 'settings:system',
  UserManage: 'user:manage',
  JobAdmin: 'job:admin',
} as const
