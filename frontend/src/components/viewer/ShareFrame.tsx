/**
 * Creating and revoking read-only links to this view (§27).
 *
 * The interface has one job the API cannot do for it: **the token is returned once and never
 * again**, because only its hash is stored. So the link has to be put in front of the operator
 * the moment it exists, with copying made the obvious next action — and the panel says plainly
 * that it will not be shown again, rather than letting someone discover that by navigating away.
 *
 * Existing links are listed dead as well as live. "Was this ever shared, and with how many
 * links" is the question someone asks after a link goes somewhere it should not have, and hiding
 * the expired ones answers it wrongly.
 */

import { Check, Copy, Link2, Trash2 } from 'lucide-react'
import { useState, type FormEvent } from 'react'

import { Badge, Button, Callout, Field, Input, Select } from '@/components/ui'
import { useCreateShareLink, useFrameShares, useRevokeShareLink } from '@/hooks/queries'
import { ApiError } from '@/lib/api'
import { formatDateTime } from '@/lib/utils'
import type { ShareLinkCreated } from '@/types/api'

/** Offered lifetimes. Short by default, because the question a link answers is usually current. */
const DURATIONS = [
  { value: '1', label: '1 day' },
  { value: '7', label: '7 days' },
  { value: '30', label: '30 days' },
  { value: '90', label: '90 days (maximum)' },
]

export function ShareFrame({ frameId }: { frameId: string }) {
  const shares = useFrameShares(frameId)
  const create = useCreateShareLink(frameId)
  const revoke = useRevokeShareLink(frameId)
  const [label, setLabel] = useState('')
  const [days, setDays] = useState('7')
  const [fresh, setFresh] = useState<ShareLinkCreated | null>(null)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const url = fresh ? `${window.location.origin}${fresh.path}` : null

  const submit = (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    setCopied(false)
    create.mutate(
      { label: label.trim() || undefined, expires_in_days: Number(days) },
      {
        onSuccess: (created) => {
          setFresh(created)
          setLabel('')
        },
        onError: (err) =>
          setError(err instanceof ApiError ? err.message : 'Could not create a link'),
      },
    )
  }

  const copy = async () => {
    if (!url) return
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
    } catch {
      // Clipboard access can be refused, and the link is on screen and selectable regardless —
      // so this is not worth an error state, only the absence of the tick.
      setCopied(false)
    }
  }

  return (
    <div className="space-y-3">
      <div>
        <p className="text-xs font-medium text-ink">Share this view</p>
        <p className="mt-0.5 text-xs text-ink-subtle">
          A read-only link to this frame on this date. Anyone holding it can open the view without
          an account, until it expires or you revoke it.
        </p>
      </div>

      <form onSubmit={submit} className="space-y-2">
        <Field label="What is it for?">
          <Input
            value={label}
            placeholder="e.g. slab query for the QS"
            maxLength={200}
            onChange={(event) => setLabel(event.target.value)}
          />
        </Field>
        <div className="flex flex-wrap items-end gap-2">
          <Field label="Expires after" className="w-44">
            <Select value={days} onChange={(event) => setDays(event.target.value)}>
              {DURATIONS.map((option) => (
                <option key={option.value} value={option.value}>
                  {option.label}
                </option>
              ))}
            </Select>
          </Field>
          <Button type="submit" size="sm" icon={Link2} loading={create.isPending}>
            Create link
          </Button>
        </div>
      </form>

      {error ? <Callout tone="bad">{error}</Callout> : null}

      {/*
        Shown once, and said so. Only the hash is stored, so navigating away really does lose it
        — the recovery is a new link and a revocation of this one, which is worth knowing before
        it happens rather than after.
      */}
      {url ? (
        <Callout tone="warn" title="Copy this now — it is not shown again">
          <span className="mb-2 block break-all rounded border border-line bg-canvas px-2 py-1.5 font-mono text-xs text-ink">
            {url}
          </span>
          <Button size="sm" variant="secondary" icon={copied ? Check : Copy} onClick={copy}>
            {copied ? 'Copied' : 'Copy link'}
          </Button>
        </Callout>
      ) : null}

      {shares.data && shares.data.length > 0 ? (
        <ul className="space-y-1.5">
          {shares.data.map((share) => (
            <li
              key={share.id}
              className="flex items-center gap-2 rounded border border-line px-2 py-1.5 text-xs"
            >
              <span className="min-w-0 flex-1">
                <span className="block truncate text-ink">{share.label ?? 'Untitled link'}</span>
                <span className="block text-[11px] text-ink-subtle">
                  {share.is_live
                    ? `expires ${formatDateTime(share.expires_at)}`
                    : share.revoked_at
                      ? 'revoked'
                      : 'expired'}
                  {share.view_count > 0
                    ? ` · opened ${share.view_count} time${share.view_count === 1 ? '' : 's'}`
                    : ' · never opened'}
                </span>
              </span>
              <Badge tone={share.is_live ? 'good' : 'neutral'}>
                {share.is_live ? 'live' : 'dead'}
              </Badge>
              {share.is_live ? (
                <Button
                  size="sm"
                  variant="ghost"
                  icon={Trash2}
                  aria-label="Revoke this link"
                  onClick={() => {
                    setError(null)
                    if (fresh?.id === share.id) setFresh(null)
                    revoke.mutate(share.id, {
                      onError: (err) =>
                        setError(err instanceof ApiError ? err.message : 'Could not revoke'),
                    })
                  }}
                />
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}
