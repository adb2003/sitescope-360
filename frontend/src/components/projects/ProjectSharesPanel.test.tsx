/**
 * The page someone opens after a share link goes somewhere it should not have.
 *
 * Two things have to be true for it to be worth opening, and both are easy to get wrong in a
 * way that looks fine: dead links must still be listed, because "was this ever shared, and how
 * widely" is part of the question; and a token must never appear, because only its hash exists
 * and any token-shaped string here would be a bug worth catching immediately.
 */

import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

import { ProjectSharesPanel } from './ProjectSharesPanel'
import type { ShareLink } from '@/types/api'

function link(overrides: Partial<ShareLink> = {}): ShareLink {
  return {
    id: 'sh-1',
    frame_id: 'fr-1',
    label: 'for the QS',
    created_by_id: 'u-1',
    expires_at: '2026-10-05T12:00:00Z',
    revoked_at: null,
    is_live: true,
    view_count: 0,
    last_viewed_at: null,
    created_at: '2026-09-28T12:00:00Z',
    ...overrides,
  }
}

function show(links: ShareLink[], total = links.length) {
  // A page, not a bare array: the list only grows, because dead links are kept on purpose.
  // `total` is separable from the rows so a short page can still report the whole count.
  vi.spyOn(globalThis, 'fetch').mockResolvedValue(
    new Response(JSON.stringify({ items: links, total, limit: 200, offset: 0 }), {
      headers: { 'content-type': 'application/json' },
    }),
  )
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <ProjectSharesPanel projectId="p-1" />
    </QueryClientProvider>,
  )
}

describe('ProjectSharesPanel', () => {
  it('lists dead links alongside live ones', async () => {
    // Hiding the expired ones answers "how widely was this shared" wrongly.
    show([
      link({ id: 'a', label: 'still out there' }),
      link({ id: 'b', label: 'stopped', is_live: false, revoked_at: '2026-09-28T13:00:00Z' }),
    ])

    expect(await screen.findByText('still out there')).toBeInTheDocument()
    expect(screen.getByText('stopped')).toBeInTheDocument()
    expect(screen.getByText(/1 live of 2 ever created/)).toBeInTheDocument()
  })

  it('offers to revoke only what is still live', async () => {
    show([link({ id: 'b', label: 'stopped', is_live: false, revoked_at: '2026-09-28T13:00:00Z' })])

    await screen.findByText('stopped')
    expect(screen.queryByRole('button', { name: /Revoke/ })).not.toBeInTheDocument()
  })

  it('says plainly when a link has never been opened', async () => {
    // A link nobody has opened can be revoked without a conversation, which is why views count.
    show([link({ view_count: 0 })])

    expect(await screen.findByText('never')).toBeInTheDocument()
  })

  it('never renders anything token-shaped', async () => {
    // Only the hash is stored, so a token here would mean the API had started returning one.
    show([link({ label: 'for the QS' })])

    await screen.findByText('for the QS')
    expect(document.body.textContent).not.toMatch(/[A-Za-z0-9_-]{30,}/)
  })

  it('counts every link ever created, not the ones on this page', async () => {
    // The list is paginated, so the rows on screen are what was fetched. "Ever created" is the
    // whole point of the sentence, and reporting the page size would understate it silently —
    // which is the specific way a leaked link gets missed.
    show([link({ id: 'a', label: 'newest' })], 87)

    expect(await screen.findByText(/of 87 ever created/)).toBeInTheDocument()
    expect(screen.getByText(/showing the newest 1/)).toBeInTheDocument()
  })

  it('says nothing about truncation when the page is the whole list', async () => {
    show([link({ id: 'a', label: 'only one' })])

    expect(await screen.findByText(/of 1 ever created/)).toBeInTheDocument()
    expect(screen.queryByText(/showing the newest/)).not.toBeInTheDocument()
  })
})
