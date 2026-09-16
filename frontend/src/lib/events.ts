/**
 * The socket that makes the processing status feel live.
 *
 * The status page polls, and will keep polling: a WebSocket is an optimisation, not a source of
 * truth. A dropped connection, a missed frame, a tab that was asleep — all of them have to be
 * survivable, and the only way to be sure of that is for the poll to remain the floor. What this
 * buys is latency: a stage that takes four seconds is currently invisible, and a finished
 * walkthrough takes up to twenty seconds to be announced.
 *
 * Three things here are deliberate and would each be easy to get wrong:
 *
 * - **The token goes in a message, not the URL.** A browser cannot set headers on
 *   `new WebSocket(url)`, and the usual workaround — `?token=…` — writes a live bearer credential
 *   into access logs, proxy logs and the browser's own history. So the socket opens, sends
 *   `{type:"auth"}`, and waits for `{type:"ready"}`.
 * - **Reconnection backs off, and gives up on a rejection.** A server that closed us for policy
 *   (1008: the token is no good) will close us again, immediately, for ever — reconnecting into
 *   that is a tight loop against our own API.
 * - **Nothing here holds application state.** An event says "this capture moved"; the caller
 *   refetches. Applying event payloads directly would make a missed message a permanently wrong
 *   screen, which is exactly what the poll is protecting against.
 */

import { tokens } from '@/lib/api'

/** What the server sends. `type` is the only field guaranteed to be present. */
export interface ServerEvent {
  type: string
  session_id?: string
  status?: string
  stage?: string
  progress?: number
  error?: string
  at?: string
}

/** Policy close: the server rejected us and will do so again. Reconnecting is pointless. */
const CLOSE_POLICY_VIOLATION = 1008

/** First retry delay. Doubles up to {@link MAX_RETRY_MS}. */
const BASE_RETRY_MS = 1_000
const MAX_RETRY_MS = 30_000

function socketUrl(): string {
  const base = import.meta.env.VITE_API_BASE_URL ?? ''
  // A relative base means "same origin as the page", which in development is the Vite dev
  // server and its proxy. Building the URL from `location` keeps http→ws and https→wss correct
  // without a second environment variable to get out of step.
  const origin = base || window.location.origin
  return `${origin.replace(/^http/, 'ws')}/api/events`
}

export interface EventStreamHandlers {
  onEvent: (event: ServerEvent) => void
  /** Called with `true` once authenticated, `false` whenever the socket is not usable. */
  onStatusChange?: (connected: boolean) => void
}

/**
 * Keep a socket open, and hand every event to `onEvent`.
 *
 * Returns a function that closes it for good. Calling it prevents any pending reconnect, which
 * matters because a React effect's cleanup runs on every dependency change — without that, a
 * remount during a backoff wait leaves two sockets racing.
 */
export function openEventStream(handlers: EventStreamHandlers): () => void {
  let socket: WebSocket | null = null
  let retryTimer: ReturnType<typeof setTimeout> | null = null
  let retryDelay = BASE_RETRY_MS
  let stopped = false

  const connect = () => {
    if (stopped) return
    const token = tokens.access
    if (!token) {
      // Signed out. Not an error and not worth retrying quickly — a sign-in will remount this.
      handlers.onStatusChange?.(false)
      return
    }

    let live: WebSocket
    try {
      live = new WebSocket(socketUrl())
    } catch {
      // Malformed URL, or sockets blocked by policy. The poll still works.
      handlers.onStatusChange?.(false)
      return
    }
    socket = live

    live.onopen = () => live.send(JSON.stringify({ type: 'auth', token }))

    live.onmessage = (message) => {
      let parsed: ServerEvent
      try {
        parsed = JSON.parse(String(message.data)) as ServerEvent
      } catch {
        return
      }
      if (parsed.type === 'ready') {
        // Only now is the connection actually working. Resetting the backoff here rather than
        // in `onopen` is the difference between "the socket opened" and "the server accepted
        // us" — a rejected token opens a socket too, and then closes it.
        retryDelay = BASE_RETRY_MS
        handlers.onStatusChange?.(true)
        return
      }
      handlers.onEvent(parsed)
    }

    live.onclose = (event) => {
      handlers.onStatusChange?.(false)
      if (stopped || event.code === CLOSE_POLICY_VIOLATION) return
      retryTimer = setTimeout(connect, retryDelay)
      retryDelay = Math.min(retryDelay * 2, MAX_RETRY_MS)
    }

    // `onerror` is always followed by `onclose`, so reconnection is handled in one place. Doing
    // it here as well is how a single failure becomes two competing sockets.
    live.onerror = () => handlers.onStatusChange?.(false)
  }

  connect()

  return () => {
    stopped = true
    if (retryTimer) clearTimeout(retryTimer)
    if (socket) {
      // Detach first: closing fires `onclose`, and a handler that schedules a reconnect would
      // undo the very thing this function exists to do.
      socket.onclose = null
      socket.onerror = null
      socket.onmessage = null
      socket.onopen = null
      try {
        socket.close()
      } catch {
        // Already closing.
      }
    }
  }
}
