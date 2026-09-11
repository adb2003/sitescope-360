/**
 * Camera connection state, shared between the camera screen and the capture screen.
 *
 * A single module-level store rather than React context, because the connection must outlive any
 * one screen: the operator connects on the camera tab, then walks the site on the capture tab,
 * and losing the client on a tab switch would mean re-probing mid-walkthrough.
 */

import { useCallback, useEffect, useState } from 'react'

import { expoCameraTransport } from './cameraTransport'
import {
  CameraError,
  discoverCamera,
  OscCameraClient,
  type CameraClient,
  type CameraInfo,
  type CameraState,
} from './osc'

export interface CameraConnection {
  client: CameraClient
  info: CameraInfo
  state: CameraState
  connectedAt: string
}

type Listener = (connection: CameraConnection | null) => void

let current: CameraConnection | null = null
const listeners = new Set<Listener>()

function publish(next: CameraConnection | null): void {
  current = next
  for (const listener of listeners) listener(next)
}

export function getCameraConnection(): CameraConnection | null {
  return current
}

export function disconnectCamera(): void {
  publish(null)
}

/** Connect by scanning the known addresses, or to one the operator typed. */
export async function connectCamera(explicitHost?: string): Promise<CameraConnection> {
  const found = explicitHost
    ? await (async () => {
        const client = new OscCameraClient(normaliseHost(explicitHost), expoCameraTransport)
        const probed = await client.probe()
        return { client, ...probed }
      })()
    : await discoverCamera(expoCameraTransport)

  const connection: CameraConnection = {
    client: found.client,
    info: found.info,
    state: found.state,
    connectedAt: new Date().toISOString(),
  }
  publish(connection)
  return connection
}

/** Re-read the camera's state. Cheap, and the only way to notice a battery about to die. */
export async function refreshCameraState(): Promise<CameraState | null> {
  if (!current) return null
  const state = await current.client.getState()
  publish({ ...current, state })
  return state
}

function normaliseHost(host: string): string {
  const trimmed = host.trim().replace(/\/$/, '')
  if (/^https?:\/\//i.test(trimmed)) return trimmed
  return `http://${trimmed}`
}

export function useCamera(): {
  connection: CameraConnection | null
  connecting: boolean
  error: string | null
  connect: (host?: string) => Promise<void>
  disconnect: () => void
  refresh: () => Promise<void>
} {
  const [connection, setConnection] = useState<CameraConnection | null>(current)
  const [connecting, setConnecting] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    listeners.add(setConnection)
    return () => {
      listeners.delete(setConnection)
    }
  }, [])

  const connect = useCallback(async (host?: string) => {
    setConnecting(true)
    setError(null)
    try {
      await connectCamera(host)
    } catch (caught) {
      // A CameraError's message is already written for the operator; anything else is not, so it
      // gets a generic sentence rather than an internal string shown on a site.
      setError(
        caught instanceof CameraError
          ? caught.message
          : 'Could not connect to a camera. Check the Wi-Fi connection and try again.',
      )
    } finally {
      setConnecting(false)
    }
  }, [])

  const refresh = useCallback(async () => {
    try {
      await refreshCameraState()
    } catch {
      // A failed refresh is not a disconnect: the camera may simply be busy writing a file.
      // Dropping the connection here would strand a recording the operator still needs to fetch.
    }
  }, [])

  return { connection, connecting, error, connect, disconnect: disconnectCamera, refresh }
}
