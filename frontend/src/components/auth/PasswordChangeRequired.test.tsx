/**
 * The screen a held account sees, and — more importantly — the application it does not see.
 *
 * The server refuses a held account everything but `/auth/me` and `/auth/change-password`, so a
 * client that renders its usual shell produces a grid of panels each reporting a 403 with the
 * one available action buried among them. The test that matters is therefore the negative one:
 * `AppShell` must replace itself, not decorate itself.
 *
 * The confirmation field is checked here too, because it is the only guard against a typo in a
 * value nobody can read back — and getting it wrong means a password the person does not know,
 * on an account that has just been held for exactly that reason.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import { AppShell } from '@/components/layout/AppShell'
import { AuthProvider } from '@/hooks/useAuth'
import { ProjectContextProvider } from '@/hooks/useProjectContext'
import { tokens } from '@/lib/api'

let fetchMock: Mock

function session(mustChange: boolean) {
  return {
    user: {
      id: 'u1',
      email: 'dana@test.example.com',
      full_name: 'Dana Whitfield',
      role: 'site_engineer',
      is_active: true,
      must_change_password: mustChange,
      last_login_at: null,
      created_at: '2026-09-01T00:00:00Z',
    },
    capabilities: ['capture:read', 'upload'],
  }
}

function show(mustChange: boolean) {
  fetchMock.mockImplementation((input: string | URL) => {
    const url = String(input)
    const body = url.includes('/auth/me')
      ? session(mustChange)
      : url.includes('/auth/change-password')
        ? { message: 'Password updated.' }
        : { items: [], total: 0, limit: 25, offset: 0 }
    return Promise.resolve(
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
    )
  })
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={['/']}>
        <ProjectContextProvider>
          <AuthProvider>
            <AppShell />
          </AuthProvider>
        </ProjectContextProvider>
      </MemoryRouter>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  tokens.set('access-token', 'refresh-token')
})

afterEach(() => {
  vi.unstubAllGlobals()
  tokens.clear()
})

describe('a held account', () => {
  it('is shown the password form instead of the application', async () => {
    show(true)

    expect(await screen.findByText('Set a new password')).toBeInTheDocument()
    // The negative half, and the reason this component replaces the shell rather than overlaying
    // it: every one of these links leads somewhere the server will refuse.
    expect(screen.queryByRole('link', { name: /Site Capture/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('link', { name: /Reports/i })).not.toBeInTheDocument()
  })

  it('is told which account was reset, so a shared machine is not a guessing game', async () => {
    show(true)

    expect(await screen.findByText(/dana@test.example.com/)).toBeInTheDocument()
  })

  it('can leave instead, because the alternative is a trap', async () => {
    show(true)

    await screen.findByText('Set a new password')
    expect(screen.getByRole('button', { name: /Sign out instead/ })).toBeInTheDocument()
  })

  it('cannot submit two different new passwords', async () => {
    show(true)
    await screen.findByText('Set a new password')

    await userEvent.type(screen.getByLabelText(/password you were given/i), 'temp-pass-1234')
    await userEvent.type(screen.getByLabelText(/^New password$/), 'chosen-pass-5678')
    await userEvent.type(screen.getByLabelText(/New password again/), 'chosen-pass-WRONG')
    await userEvent.click(screen.getByRole('button', { name: /Set password/ }))

    expect(await screen.findByText(/do not match/)).toBeInTheDocument()
    const sent = fetchMock.mock.calls.some(([url]) => String(url).includes('change-password'))
    expect(sent).toBe(false)
  })

  it('sends the change when both new passwords agree', async () => {
    show(true)
    await screen.findByText('Set a new password')

    await userEvent.type(screen.getByLabelText(/password you were given/i), 'temp-pass-1234')
    await userEvent.type(screen.getByLabelText(/^New password$/), 'chosen-pass-5678')
    await userEvent.type(screen.getByLabelText(/New password again/), 'chosen-pass-5678')
    await userEvent.click(screen.getByRole('button', { name: /Set password/ }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(([url]) => String(url).includes('change-password'))
      expect(call).toBeTruthy()
      const body = JSON.parse(String((call?.[1] as RequestInit).body))
      expect(body).toEqual({
        current_password: 'temp-pass-1234',
        new_password: 'chosen-pass-5678',
      })
    })
  })
})

describe('an ordinary account', () => {
  it('sees the application and never the password form', async () => {
    show(false)

    expect(await screen.findByRole('link', { name: /Site Capture/i })).toBeInTheDocument()
    expect(screen.queryByText('Set a new password')).not.toBeInTheDocument()
  })
})
