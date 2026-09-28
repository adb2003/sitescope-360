/**
 * A shared 360 view, for someone with no account (§27).
 *
 * This page is outside the application shell on purpose. Whoever opens it has a link and nothing
 * else — no project, no sidebar to navigate, no sign-in they are expected to complete — and a
 * page that showed them chrome leading everywhere they cannot go would read as a broken login.
 *
 * It also fetches **without the API client's auth layer**: `api` attaches a bearer token and
 * refreshes on 401, and neither applies here. A visitor with a stale token in localStorage from
 * some other deployment must not have it sent, and a 404 from a dead link must not be mistaken
 * for a session that needs renewing.
 *
 * The caveats come from the server's own `notice` rather than being written here. The same words
 * then appear wherever this payload is rendered, and a client cannot present a shared capture as
 * a live view of a project by omitting them.
 */

import { AlertTriangle, Clock } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useParams } from 'react-router-dom'

import { PanoramaCanvas } from '@/components/viewer/PanoramaCanvas'
import { Callout, Skeleton } from '@/components/ui'
import { formatDateTime } from '@/lib/utils'

interface SharedView {
  captured_at: string
  project_name: string | null
  building_name: string | null
  floor_name: string | null
  location_code: string | null
  width_px: number
  height_px: number
  initial_yaw_deg: number
  full: { url: string } | null
  preview: { url: string } | null
  expires_at: string
  notice: string
}

type State =
  | { status: 'loading' }
  | { status: 'ready'; view: SharedView }
  /** One state for every refusal, mirroring the server, which answers them all identically. */
  | { status: 'gone' }

export function SharedViewPage() {
  const { token } = useParams<{ token: string }>()
  const [state, setState] = useState<State>({ status: 'loading' })

  useEffect(() => {
    if (!token) {
      setState({ status: 'gone' })
      return
    }
    let cancelled = false
    // Deliberately `fetch` and not the app's `api` client: no bearer token, no refresh-on-401.
    fetch(`/api/shared/${encodeURIComponent(token)}`)
      .then((response) => (response.ok ? response.json() : Promise.reject(response.status)))
      .then((view: SharedView) => {
        if (!cancelled) setState({ status: 'ready', view })
      })
      .catch(() => {
        if (!cancelled) setState({ status: 'gone' })
      })
    return () => {
      cancelled = true
    }
  }, [token])

  if (state.status === 'loading') {
    return (
      <div className="min-h-screen bg-canvas p-4">
        <Skeleton className="h-[70vh] w-full" />
      </div>
    )
  }

  if (state.status === 'gone') {
    return (
      <div className="grid min-h-screen place-items-center bg-canvas p-6">
        <div className="max-w-md space-y-3 text-center">
          <AlertTriangle className="mx-auto size-8 text-warn" />
          <h1 className="text-lg font-medium text-ink">This link is not available</h1>
          {/*
            One message for every case, matching the server. It does not say "expired", because
            that would confirm the link was once real — useful only to someone guessing, and no
            help to the person holding it, who has to ask the sender either way.
          */}
          <p className="text-sm text-ink-subtle">
            It may have been revoked, or it may never have been valid. Ask whoever sent it for a
            new link.
          </p>
        </div>
      </div>
    )
  }

  const { view } = state
  const place = [view.project_name, view.building_name, view.floor_name, view.location_code]
    .filter(Boolean)
    .join(' · ')

  return (
    <div className="min-h-screen bg-canvas">
      <header className="border-b border-line px-4 py-3">
        <p className="text-sm font-medium text-ink">{place || 'Shared 360 view'}</p>
        <p className="tnum text-xs text-ink-subtle">
          Captured {formatDateTime(view.captured_at)}
        </p>
      </header>

      <main className="space-y-3 p-4">
        {view.full ? (
          <PanoramaCanvas
            src={view.full.url}
            previewSrc={view.preview?.url ?? null}
            initialYaw={view.initial_yaw_deg}
            className="h-[70vh] w-full rounded border border-line"
          />
        ) : (
          <Callout tone="warn">The image for this view could not be loaded.</Callout>
        )}

        <Callout tone="info" title="About this view">
          <span className="block">{view.notice}</span>
          <span className="mt-1 flex items-center gap-1.5 text-xs text-ink-subtle">
            <Clock className="size-3.5" />
            Link expires {formatDateTime(view.expires_at)}
          </span>
        </Callout>
      </main>
    </div>
  )
}
