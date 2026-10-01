/**
 * Every share link into this project, in one place (§27).
 *
 * The API for this existed before the page did, which made it useless in the moment it matters:
 * a link has gone somewhere it should not have, and the question is *what else is out there* —
 * not "what is shared from this one frame", which is all the viewer could answer, one frame at a
 * time.
 *
 * Dead links are listed alongside live ones. "Was this ever shared, and how widely" is part of
 * the same question, and a list that quietly dropped the expired ones would answer it wrongly.
 * They are visibly dead rather than hidden.
 *
 * Nothing here can show a token: only its hash is stored. What it can do is name a link, say
 * whether it still works, say whether anyone ever opened it, and stop it.
 */

import { Link2, Trash2 } from 'lucide-react'
import { useState } from 'react'

import {
  Badge,
  Button,
  Callout,
  Card,
  CardHeader,
  EmptyState,
  Skeleton,
  Table,
  Td,
  Th,
  Tr,
} from '@/components/ui'
import { useProjectShares, useRevokeProjectShare } from '@/hooks/queries'
import { ApiError } from '@/lib/api'
import { formatDateTime } from '@/lib/utils'
import type { ShareLink } from '@/types/api'

/** Live first, then most recently created — what is still out there is the urgent half. */
function ordered(links: ShareLink[]): ShareLink[] {
  return [...links].sort((a, b) => {
    if (a.is_live !== b.is_live) return a.is_live ? -1 : 1
    return b.created_at.localeCompare(a.created_at)
  })
}

function statusOf(link: ShareLink): { tone: 'good' | 'neutral'; label: string; detail: string } {
  if (link.is_live) {
    return {
      tone: 'good',
      label: 'live',
      detail: `expires ${formatDateTime(link.expires_at)}`,
    }
  }
  return {
    tone: 'neutral',
    label: link.revoked_at ? 'revoked' : 'expired',
    detail: link.revoked_at
      ? `revoked ${formatDateTime(link.revoked_at)}`
      : `expired ${formatDateTime(link.expires_at)}`,
  }
}

export function ProjectSharesPanel({ projectId }: { projectId: string }) {
  const shares = useProjectShares(projectId)
  const revoke = useRevokeProjectShare(projectId)
  const [error, setError] = useState<string | null>(null)

  const links = shares.data ? ordered(shares.data.items) : []
  const liveCount = links.filter((link) => link.is_live).length

  return (
    <Card>
      <CardHeader
        title="Shared links"
        description="Read-only links to a single view. Anyone holding one can open it without an account."
      />

      <div className="p-3.5 pt-0">
        {error ? <Callout tone="bad">{error}</Callout> : null}

        {shares.isLoading ? (
          <Skeleton className="h-24" />
        ) : links.length === 0 ? (
          <EmptyState
            icon={Link2}
            title="Nothing has been shared from this project"
            description="Links are created from a frame in the 360 viewer."
          />
        ) : (
          <>
            {/*
              `total` and not `links.length`. The list is paginated, so the number of rows on
              screen is how many were fetched — and "ever created" is the whole point of the
              sentence. Reporting the page size would understate it the moment a project has
              more links than one page.
            */}
            <p className="mb-2 text-xs text-ink-subtle">
              {liveCount} live of {shares.data?.total ?? links.length} ever created
              {links.length < (shares.data?.total ?? 0) ? `, showing the newest ${links.length}` : ''}.
            </p>
            <Table>
              <thead>
                <Tr>
                  <Th>Link</Th>
                  <Th>Status</Th>
                  <Th>Opened</Th>
                  <Th />
                </Tr>
              </thead>
              <tbody>
                {links.map((link) => {
                  const status = statusOf(link)
                  return (
                    <Tr key={link.id}>
                      <Td>
                        <span className="block text-ink">{link.label ?? 'Untitled link'}</span>
                        <span className="block text-xs text-ink-subtle">
                          created {formatDateTime(link.created_at)}
                        </span>
                      </Td>
                      <Td>
                        <Badge tone={status.tone}>{status.label}</Badge>
                        <span className="mt-0.5 block text-[11px] text-ink-subtle">
                          {status.detail}
                        </span>
                      </Td>
                      <Td className="tnum text-xs text-ink-muted">
                        {/*
                          A link nobody has ever opened can be revoked without a conversation,
                          which is the whole reason views are counted.
                        */}
                        {link.view_count > 0
                          ? `${link.view_count}× · last ${formatDateTime(link.last_viewed_at)}`
                          : 'never'}
                      </Td>
                      <Td className="text-right">
                        {link.is_live ? (
                          <Button
                            size="sm"
                            variant="ghost"
                            icon={Trash2}
                            loading={revoke.isPending}
                            aria-label={`Revoke ${link.label ?? 'this link'}`}
                            onClick={() => {
                              setError(null)
                              revoke.mutate(link.id, {
                                onError: (err) =>
                                  setError(
                                    err instanceof ApiError ? err.message : 'Could not revoke',
                                  ),
                              })
                            }}
                          >
                            Revoke
                          </Button>
                        ) : null}
                      </Td>
                    </Tr>
                  )
                })}
              </tbody>
            </Table>
          </>
        )}
      </div>
    </Card>
  )
}
