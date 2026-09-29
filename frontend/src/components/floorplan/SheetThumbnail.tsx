/**
 * One sheet's preview image, fetched with credentials.
 *
 * A drawing set is project data, so the preview endpoint is authenticated like everything else —
 * and an `<img src>` cannot send an Authorization header. So the bytes are fetched through the
 * ordinary API client (which carries the token and handles a 401 refresh) and handed to the
 * image as an object URL.
 *
 * It is also genuinely lazy. `loading="lazy"` would do nothing here, because the fetch is ours
 * rather than the browser's — so an IntersectionObserver holds the request back until the tile
 * is actually near the viewport. That matters: a sheet set can run to hundreds of pages, and a
 * picker that fired a request per page on open would be slower than the guessing it replaces.
 */

import { FileWarning } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { request } from '@/lib/api'

type State = 'waiting' | 'loading' | 'ready' | 'failed'

export function SheetThumbnail({ planId, page }: { planId: string; page: number }) {
  const holder = useRef<HTMLDivElement | null>(null)
  const [state, setState] = useState<State>('waiting')
  const [url, setUrl] = useState<string | null>(null)

  // Come into view first.
  useEffect(() => {
    const node = holder.current
    if (!node || state !== 'waiting') return
    // Fail *open* when visibility cannot be determined. An observer answers "is this on screen",
    // and where nothing can answer — no IntersectionObserver at all, or a viewport with no
    // height, which is what an embedded or headless pane reports — the honest fallback is to
    // load. Waiting for an event that can never arrive leaves a permanently blank picker with
    // no way for the operator to recover; loading early costs one request.
    const cannotObserve =
      typeof IntersectionObserver === 'undefined' ||
      typeof window === 'undefined' ||
      window.innerHeight === 0
    if (cannotObserve) {
      setState('loading')
      return
    }
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) {
          setState('loading')
          observer.disconnect()
        }
      },
      { rootMargin: '200px' },
    )
    observer.observe(node)
    return () => observer.disconnect()
  }, [state])

  // Then fetch, and clean up the object URL so a long picker session does not leak blobs.
  useEffect(() => {
    if (state !== 'loading') return
    let revoked: string | null = null
    let cancelled = false

    request<Response>(`/floorplans/${planId}/sheets/${page}/preview`, { raw: true })
      .then((response) => response.blob())
      .then((blob) => {
        if (cancelled) return
        revoked = URL.createObjectURL(blob)
        setUrl(revoked)
        setState('ready')
      })
      .catch(() => {
        if (!cancelled) setState('failed')
      })

    return () => {
      cancelled = true
      if (revoked) URL.revokeObjectURL(revoked)
    }
  }, [state, planId, page])

  return (
    <div ref={holder} className="aspect-[1/1.4] w-full bg-surface">
      {state === 'ready' && url ? (
        <img src={url} alt={`Sheet ${page}`} className="size-full object-contain" />
      ) : state === 'failed' ? (
        <span className="grid size-full place-items-center text-ink-subtle">
          <FileWarning className="size-5" />
        </span>
      ) : (
        <span className="block size-full animate-pulse bg-surface-raised" />
      )}
    </div>
  )
}
