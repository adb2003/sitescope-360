/**
 * Turn pushed events into query invalidations (§34).
 *
 * Mounted once, high in the tree, for the whole signed-in session. One socket per tab rather than
 * one per component: a capture page, a viewer and a dashboard all care about the same events, and
 * three sockets would mean three subscriptions on the server for the same tab.
 *
 * What arrives is a hint, never data. `{type:"capture.status", session_id}` means "that capture
 * moved, ask again" — so this invalidates and lets TanStack Query refetch through the normal
 * authorised endpoints. Writing event payloads straight into the cache would be faster and would
 * make a single missed message a permanently wrong screen; the poll underneath exists precisely
 * so that cannot happen, and this must not undermine it.
 */

import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useState } from 'react'

import { openEventStream, type ServerEvent } from '@/lib/events'
import { keys } from '@/hooks/queries'

export interface CaptureEventsState {
  /** True while the socket is authenticated and delivering. The UI may say so; it must not rely on it. */
  connected: boolean
}

export function useCaptureEvents(enabled: boolean): CaptureEventsState {
  const queryClient = useQueryClient()
  const [connected, setConnected] = useState(false)

  const onEvent = useCallback(
    (event: ServerEvent) => {
      if (event.type !== 'capture.status' || !event.session_id) return
      const sessionId = event.session_id

      // Everything a status change can alter. Listed explicitly rather than clearing the whole
      // cache: a blanket invalidation would refetch every dashboard, timeline and coverage query
      // in the tab on each of the six stage transitions of a single walkthrough.
      void queryClient.invalidateQueries({ queryKey: keys.captureStatus(sessionId) })
      void queryClient.invalidateQueries({ queryKey: keys.capture(sessionId) })
      void queryClient.invalidateQueries({ queryKey: keys.captureFiles(sessionId) })
      // Partial key: the frames query is parameterised by filters, so this catches every
      // variant the tab happens to be holding.
      void queryClient.invalidateQueries({ queryKey: ['capture', sessionId, 'frames'] })

      // The list shows each capture's status and frame count, so it goes stale on every
      // transition too — and it is usually the page the operator is watching from.
      void queryClient.invalidateQueries({ queryKey: ['captures'] })

      // Only on the terminal transitions: these are the ones that change the floor plan and the
      // dashboards, and re-running them mid-pipeline would be work for a result that has not
      // changed yet.
      if (event.status === 'ready' || event.status === 'failed') {
        void queryClient.invalidateQueries({ queryKey: keys.capturePath(sessionId) })
        void queryClient.invalidateQueries({ queryKey: ['floor'] })
        void queryClient.invalidateQueries({ queryKey: ['project'] })
        void queryClient.invalidateQueries({ queryKey: keys.portfolio })
      }
    },
    [queryClient],
  )

  useEffect(() => {
    if (!enabled) {
      setConnected(false)
      return
    }
    return openEventStream({ onEvent, onStatusChange: setConnected })
  }, [enabled, onEvent])

  return { connected }
}
