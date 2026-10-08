/**
 * The panel where somebody decides what a person is **on this job** (§27).
 *
 * A role used to belong to the account, so this panel could only grant or remove access. Now it
 * sets the role, and three things have to be true for it to be honest about a rule the server
 * will enforce either way:
 *
 *   * the roles on offer stop at the actor's own role here — a manager who could choose "admin"
 *     from this list would be choosing a 403;
 *   * a stored role above the person's account role is shown as capped, because the API enforces
 *     `min(account, project)` and a row reading `project_manager` for a `viewer` account would
 *     otherwise look like a permission they have;
 *   * somebody who cannot manage this project gets no controls at all, from the project's role
 *     rather than the account's — which is the case the account check gets wrong.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from 'vitest'

import { ProjectAccessPanel } from './ProjectAccessPanel'
import { AuthProvider } from '@/hooks/useAuth'
import { tokens } from '@/lib/api'
import type { ProjectMember, Role } from '@/types/api'

let fetchMock: Mock

const ACCOUNT = {
  user: {
    id: 'u-me',
    email: 'pm@test.example.com',
    full_name: 'Pat Manager',
    role: 'project_manager',
    is_active: true,
    last_login_at: null,
    created_at: '2026-09-01T00:00:00Z',
  },
  capabilities: [
    'project:read',
    'project:write',
    'capture:read',
    'settings:project',
    'floorplan:edit',
    'report:generate',
  ],
}

function member(overrides: Partial<ProjectMember> = {}): ProjectMember {
  return {
    user_id: 'u-1',
    email: 'sam@example.com',
    full_name: 'Sam Engineer',
    role: 'site_engineer',
    account_role: 'site_engineer',
    effective_role: 'site_engineer',
    is_active: true,
    added_at: '2026-09-20T09:00:00Z',
    added_by_id: 'u-me',
    ...overrides,
  }
}

function show(members: ProjectMember[], yourRole: Role = 'project_manager') {
  fetchMock.mockImplementation((input: string | URL) => {
    const url = String(input)
    const body = url.includes('/members')
      ? members
      : url.includes('/auth/me')
        ? ACCOUNT
        : url.includes('/projects/p-1')
          ? {
              id: 'p-1',
              your_role: yourRole,
              // What the server sends: the capability list for the role held *here*.
              your_capabilities:
                yourRole === 'project_manager'
                  ? ACCOUNT.capabilities
                  : ['project:read', 'capture:read'],
            }
          : undefined
    if (body === undefined) {
      return Promise.resolve(new Response('{}', { status: 404 }))
    }
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
  render(
    <QueryClientProvider client={client}>
      <AuthProvider>
        <ProjectAccessPanel projectId="p-1" />
      </AuthProvider>
    </QueryClientProvider>,
  )
}

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
  tokens.set('access-token', 'refresh-token')
})

afterEach(() => {
  tokens.clear()
  vi.unstubAllGlobals()
})

describe('ProjectAccessPanel', () => {
  it('offers no role above the actor’s own on this project', async () => {
    show([member()])

    const select = await screen.findByRole('combobox', {
      name: /Role for Sam Engineer on this project/,
    })
    const offered = Array.from(select.querySelectorAll('option')).map((option) => option.value)

    expect(offered).toEqual(['viewer', 'site_engineer', 'project_manager'])
    expect(offered).not.toContain('admin')
  })

  it('says when a role is capped by the account behind it', async () => {
    // The case that looks like a bug in the API rather than a rule: stored as a manager, in force
    // as a viewer, because the account was lowered after they were put on the job.
    show([
      member({
        role: 'project_manager',
        account_role: 'viewer',
        effective_role: 'viewer',
      }),
    ])

    expect(await screen.findByText(/Capped to Viewer/)).toBeInTheDocument()
    expect(screen.getByText(/their account is Viewer/)).toBeInTheDocument()
  })

  it('sends the new role when one is chosen', async () => {
    show([member()])

    const select = await screen.findByRole('combobox', {
      name: /Role for Sam Engineer on this project/,
    })
    await userEvent.selectOptions(select, 'viewer')

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        ([url, init]) => String(url).includes('/members/u-1') && (init as RequestInit)?.method === 'PATCH',
      )
      expect(call).toBeTruthy()
      expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({ role: 'viewer' })
    })
  })

  it('withholds every control from somebody who only views this project', async () => {
    // The account holds `settings:project` — it is a project manager — and holds it elsewhere.
    // Here they are a viewer, and the panel has to read the project rather than the account.
    show([member()], 'viewer')

    expect(await screen.findByText('Sam Engineer')).toBeInTheDocument()
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Grant access/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Remove Sam Engineer/ })).not.toBeInTheDocument()
    // Still readable: who is on the job is not a secret from the people on it.
    expect(screen.getByText('Site engineer')).toBeInTheDocument()
  })

  it('grants access as the role that was chosen', async () => {
    show([])

    await screen.findByRole('button', { name: /Grant access/ })
    await userEvent.type(screen.getByPlaceholderText('colleague@example.com'), 'new@example.com')
    await userEvent.selectOptions(screen.getByRole('combobox', { name: /Role on this project/ }), 'viewer')
    await userEvent.click(screen.getByRole('button', { name: /Grant access/ }))

    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        ([url, init]) => String(url).includes('/members') && (init as RequestInit)?.method === 'POST',
      )
      expect(call).toBeTruthy()
      expect(JSON.parse(String((call?.[1] as RequestInit).body))).toEqual({
        email: 'new@example.com',
        role: 'viewer',
      })
    })
  })
})
