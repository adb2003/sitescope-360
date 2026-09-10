import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api, buildUrl, request, tokens } from '@/lib/api'

describe('buildUrl', () => {
  it('prefixes the API path', () => {
    expect(buildUrl('/projects')).toBe('/api/projects')
  })

  it('serialises query parameters and drops empty ones', () => {
    const url = buildUrl('/projects', { q: 'chevron', limit: 10, status: null, empty: '' })
    expect(url).toContain('q=chevron')
    expect(url).toContain('limit=10')
    expect(url).not.toContain('status')
    expect(url).not.toContain('empty')
  })

  it('repeats a key for array values, matching FastAPI list queries', () => {
    const url = buildUrl('/captures', { stages: ['validate', 'quality'] })
    expect(url).toContain('stages=validate')
    expect(url).toContain('stages=quality')
  })

  it('encodes values that would otherwise break the query string', () => {
    expect(buildUrl('/projects', { q: 'a&b=c' })).toContain('q=a%26b%3Dc')
  })
})

describe('ApiError', () => {
  it('extracts field-level messages from a 422 envelope', () => {
    const error = new ApiError(422, {
      code: 'validation_error',
      message: 'The request could not be validated',
      details: {
        fields: [
          { field: 'code', message: 'String should match pattern', type: 'string_pattern' },
          { field: 'name', message: 'Field required', type: 'missing' },
        ],
      },
    })

    expect(error.fieldErrors).toEqual({
      code: 'String should match pattern',
      name: 'Field required',
    })
    expect(error.isAuthError).toBe(false)
  })

  it('returns an empty map when there are no field errors', () => {
    const error = new ApiError(409, { code: 'conflict', message: 'Already exists' })
    expect(error.fieldErrors).toEqual({})
  })

  it('classifies auth and permission failures', () => {
    expect(new ApiError(401, { code: 'a', message: 'b' }).isAuthError).toBe(true)
    expect(new ApiError(403, { code: 'a', message: 'b' }).isPermissionError).toBe(true)
  })
})

describe('request', () => {
  const fetchMock = vi.fn()

  beforeEach(() => {
    tokens.clear()
    fetchMock.mockReset()
    vi.stubGlobal('fetch', fetchMock)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  function jsonResponse(body: unknown, status = 200): Response {
    return new Response(JSON.stringify(body), {
      status,
      headers: { 'content-type': 'application/json' },
    })
  }

  it('attaches the access token', async () => {
    tokens.set('access-1', 'refresh-1')
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }))

    await api.get('/projects')

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer access-1')
  })

  it('omits the token for anonymous requests', async () => {
    tokens.set('access-1', 'refresh-1')
    fetchMock.mockResolvedValue(jsonResponse({ ok: true }))

    await api.anonymous.post('/auth/login', { email: 'a@example.com' })

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined()
  })

  it('throws a typed ApiError carrying the backend envelope', async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({ code: 'not_found', message: 'Project x not found' }, 404),
    )

    await expect(api.get('/projects/x')).rejects.toMatchObject({
      name: 'ApiError',
      status: 404,
      code: 'not_found',
      message: 'Project x not found',
    })
  })

  it('refreshes once on a 401 and retries the original request', async () => {
    tokens.set('stale-access', 'good-refresh')

    fetchMock
      .mockResolvedValueOnce(jsonResponse({ code: 'authentication_failed', message: 'expired' }, 401))
      .mockResolvedValueOnce(
        jsonResponse({ access_token: 'fresh-access', refresh_token: 'fresh-refresh' }),
      )
      .mockResolvedValueOnce(jsonResponse({ id: 'p1' }))

    const result = await api.get<{ id: string }>('/projects/p1')

    expect(result).toEqual({ id: 'p1' })
    expect(fetchMock).toHaveBeenCalledTimes(3)
    expect(fetchMock.mock.calls[1]?.[0]).toContain('/auth/refresh')
    // The retry must use the new token, not the stale one.
    const retryInit = fetchMock.mock.calls[2]?.[1] as RequestInit
    expect((retryInit.headers as Record<string, string>).Authorization).toBe('Bearer fresh-access')
    expect(tokens.access).toBe('fresh-access')
  })

  it('clears the session and notifies when the refresh itself fails', async () => {
    tokens.set('stale-access', 'dead-refresh')
    const onUnauthorized = vi.fn()
    const { setUnauthorizedHandler } = await import('@/lib/api')
    setUnauthorizedHandler(onUnauthorized)

    fetchMock
      .mockResolvedValueOnce(jsonResponse({ code: 'authentication_failed', message: 'x' }, 401))
      .mockResolvedValueOnce(jsonResponse({ code: 'authentication_failed', message: 'x' }, 401))
      .mockResolvedValueOnce(jsonResponse({ code: 'authentication_failed', message: 'x' }, 401))

    await expect(api.get('/projects')).rejects.toBeInstanceOf(ApiError)
    expect(onUnauthorized).toHaveBeenCalled()
    expect(tokens.access).toBeNull()
    setUnauthorizedHandler(null)
  })

  it('does not set Content-Type for multipart uploads', async () => {
    // The browser must choose the boundary itself; setting the header breaks the upload.
    tokens.set('access-1', 'refresh-1')
    fetchMock.mockResolvedValue(jsonResponse([]))

    const form = new FormData()
    form.append('files', new File(['x'], 'a.jpg', { type: 'image/jpeg' }))
    await api.upload('/captures/CAP-1/upload', form)

    const init = fetchMock.mock.calls[0]?.[1] as RequestInit
    expect((init.headers as Record<string, string>)['Content-Type']).toBeUndefined()
    expect(init.body).toBeInstanceOf(FormData)
  })

  it('reports a readable message when the server cannot be reached', async () => {
    fetchMock.mockResolvedValue(new Response('gateway down', { status: 502 }))
    await expect(request('/projects')).rejects.toMatchObject({ status: 502 })
  })
})
