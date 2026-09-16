/**
 * The event socket's connection rules.
 *
 * Push is an optimisation, so none of these failures break a page — the poll underneath still
 * arrives. What they break is the server, or the tab:
 *
 * - reconnecting after a policy close is a tight loop against our own API, from every open tab,
 *   for as long as the token stays bad;
 * - not detaching handlers before closing means a React effect's cleanup schedules the very
 *   reconnect it was supposed to prevent, and a remount leaves two sockets racing;
 * - resetting the backoff when the *socket* opens rather than when the server accepts us makes
 *   a rejected token retry at one second for ever, which is the same loop by another route.
 *
 * `WebSocket` is replaced with a fake so each of those is observable rather than inferred.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { openEventStream } from './events'
import { tokens } from './api'

class FakeSocket {
  static instances: FakeSocket[] = []

  url: string
  sent: string[] = []
  closed = false
  onopen: (() => void) | null = null
  onmessage: ((event: { data: string }) => void) | null = null
  onclose: ((event: { code: number }) => void) | null = null
  onerror: (() => void) | null = null

  constructor(url: string) {
    this.url = url
    FakeSocket.instances.push(this)
  }

  send(data: string) {
    this.sent.push(data)
  }

  close() {
    this.closed = true
  }

  /** Drive the handshake the way the server does. */
  accept() {
    this.onopen?.()
    this.onmessage?.({ data: JSON.stringify({ type: 'ready' }) })
  }

  deliver(event: unknown) {
    this.onmessage?.({ data: JSON.stringify(event) })
  }

  serverClose(code: number) {
    this.onclose?.({ code })
  }
}

const latest = () => FakeSocket.instances[FakeSocket.instances.length - 1]!

beforeEach(() => {
  FakeSocket.instances = []
  vi.useFakeTimers()
  vi.stubGlobal('WebSocket', FakeSocket as unknown as typeof WebSocket)
  vi.spyOn(tokens, 'access', 'get').mockReturnValue('a-token')
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('openEventStream', () => {
  it('authenticates with a message rather than the URL', () => {
    // A token in the query string is a live bearer credential in every access log, proxy log
    // and the browser's own history.
    const stop = openEventStream({ onEvent: () => undefined })

    const socket = latest()
    expect(socket.url).not.toContain('a-token')
    socket.onopen?.()
    expect(JSON.parse(socket.sent[0]!)).toEqual({ type: 'auth', token: 'a-token' })
    stop()
  })

  it('opens a ws:// url on the page origin', () => {
    const stop = openEventStream({ onEvent: () => undefined })

    expect(latest().url).toMatch(/^wss?:\/\/.+\/api\/events$/)
    stop()
  })

  it('reports connected only once the server says ready', () => {
    // Opening proves nothing: a socket with a rejected token opens too, and is then closed.
    const states: boolean[] = []
    const stop = openEventStream({
      onEvent: () => undefined,
      onStatusChange: (value) => states.push(value),
    })

    latest().onopen?.()
    expect(states).toEqual([])

    latest().onmessage?.({ data: JSON.stringify({ type: 'ready' }) })
    expect(states).toEqual([true])
    stop()
  })

  it('passes events to the caller but not the ready handshake', () => {
    const seen: unknown[] = []
    const stop = openEventStream({ onEvent: (event) => seen.push(event) })
    latest().accept()

    latest().deliver({ type: 'capture.status', session_id: 'CAP-1', status: 'ready' })

    expect(seen).toEqual([{ type: 'capture.status', session_id: 'CAP-1', status: 'ready' }])
    stop()
  })

  it('ignores a frame that is not JSON instead of throwing', () => {
    const seen: unknown[] = []
    const stop = openEventStream({ onEvent: (event) => seen.push(event) })
    latest().accept()

    latest().onmessage?.({ data: 'not json' })

    expect(seen).toEqual([])
    stop()
  })

  it('reconnects after a network close, backing off', () => {
    const stop = openEventStream({ onEvent: () => undefined })
    latest().accept()

    latest().serverClose(1006)
    expect(FakeSocket.instances).toHaveLength(1)
    vi.advanceTimersByTime(1000)
    expect(FakeSocket.instances).toHaveLength(2)

    // Second failure waits longer. A fixed delay is a synchronised retry storm from every tab.
    latest().serverClose(1006)
    vi.advanceTimersByTime(1000)
    expect(FakeSocket.instances).toHaveLength(2)
    vi.advanceTimersByTime(1000)
    expect(FakeSocket.instances).toHaveLength(3)
    stop()
  })

  it('does not reconnect after a policy close', () => {
    // 1008 means the server rejected the token and will reject it again. Retrying is a tight
    // loop against our own API from every open tab.
    const stop = openEventStream({ onEvent: () => undefined })

    latest().serverClose(1008)
    vi.advanceTimersByTime(60_000)

    expect(FakeSocket.instances).toHaveLength(1)
    stop()
  })

  it('resets the backoff only after a successful handshake', () => {
    const stop = openEventStream({ onEvent: () => undefined })
    latest().accept()
    latest().serverClose(1006)
    vi.advanceTimersByTime(1000)
    // Second socket fails without ever being accepted, so the delay must keep growing.
    latest().serverClose(1006)
    vi.advanceTimersByTime(1999)
    expect(FakeSocket.instances).toHaveLength(2)
    vi.advanceTimersByTime(1)
    expect(FakeSocket.instances).toHaveLength(3)

    // Now it succeeds, and the next failure starts from the base delay again.
    latest().accept()
    latest().serverClose(1006)
    vi.advanceTimersByTime(1000)

    expect(FakeSocket.instances).toHaveLength(4)
    stop()
  })

  it('stopping cancels a pending reconnect', () => {
    // A React effect's cleanup runs on every dependency change. Without this, a remount during
    // a backoff wait leaves two sockets racing, both subscribed on the server.
    const stop = openEventStream({ onEvent: () => undefined })
    latest().serverClose(1006)

    stop()
    vi.advanceTimersByTime(60_000)

    expect(FakeSocket.instances).toHaveLength(1)
  })

  it('stopping detaches the close handler before closing', () => {
    // Otherwise `close()` fires `onclose`, which schedules the reconnect that `stop` exists to
    // prevent — and the socket count grows every time the component unmounts.
    const stop = openEventStream({ onEvent: () => undefined })
    latest().accept()

    stop()
    vi.advanceTimersByTime(60_000)

    expect(latest().closed).toBe(true)
    expect(FakeSocket.instances).toHaveLength(1)
  })

  it('does not open a socket when there is no token', () => {
    vi.spyOn(tokens, 'access', 'get').mockReturnValue(null)

    const stop = openEventStream({ onEvent: () => undefined })

    expect(FakeSocket.instances).toHaveLength(0)
    stop()
  })
})
