/**
 * Open Spherical Camera (OSC) client (§6, §7, §32).
 *
 * ## Why this lives in the mobile app and not the backend
 *
 * A 360 camera exposes its HTTP API over its own Wi-Fi access point. To reach it, the phone must
 * *join that access point* — at which point the phone has no route to the internet and therefore
 * no route to the platform API. So the sequence is necessarily:
 *
 *   1. phone joins the camera's Wi-Fi
 *   2. phone drives the camera: status, start, stop, list, download   <- this file
 *   3. phone stores the file locally and queues it                    <- lib/queue.ts
 *   4. phone rejoins a network with internet; the queue uploads       <- lib/queue.ts
 *
 * The backend's `Generic360Provider` speaks the same protocol, and it is still the right place
 * for a camera on the site LAN. It cannot serve this flow, because the server is never the device
 * on the camera's private network. Both exist deliberately.
 *
 * ## Why OSC rather than a vendor SDK
 *
 * Open Spherical Camera is an **open, published specification**. Ricoh Theta and several other
 * 360 cameras implement it directly. No proprietary SDK, no vendor account, no per-device
 * licence — which is the constraint this whole platform is built under. A camera that speaks only
 * a closed protocol needs its own adapter behind {@link CameraClient}; it does not get to change
 * anything above this file.
 *
 * ## What is verified and what is not
 *
 * The protocol handling here is written to the published specification and is covered by tests
 * against a fake transport. It has **not** been run against physical hardware — there is no 360
 * camera on this project. Every place where a real device may differ from the spec is marked, and
 * {@link CameraClient.probe} reports exactly what it found rather than assuming a model.
 */

/** Default AP addresses. The camera is the gateway on its own network. */
export const KNOWN_CAMERA_HOSTS = [
  { host: 'http://192.168.1.1', hint: 'Ricoh Theta (default AP address)' },
  { host: 'http://192.168.42.1', hint: 'Insta360 and several others' },
  { host: 'http://192.168.0.1', hint: 'Generic camera AP gateway' },
] as const

/** OSC calls are quick; a hanging socket means the phone is not on the camera's network. */
const REQUEST_TIMEOUT_MS = 6000
/** Starting a recording can take a moment while the camera spins up. */
const COMMAND_TIMEOUT_MS = 15000
/** A 360 video is large; the download needs a far longer ceiling. */
const DOWNLOAD_TIMEOUT_MS = 15 * 60 * 1000

export interface CameraInfo {
  manufacturer: string
  model: string
  serialNumber: string | null
  firmwareVersion: string | null
  /** Straight from the device: what it says it can do. Never inferred from the model name. */
  apiLevel: number[]
  /**
   * Whether the device *advertises* video, i.e. declares OSC API level 2.
   *
   * Not the same question as "can it record video". Level 1 cameras vary by vendor and the spec
   * gives no reliable way to ask, so this is `false` for them even when they can. The capture
   * flow therefore attempts video anyway and surfaces the camera's own refusal — an honest
   * unknown beats a guess that hides a working button or promises one that fails.
   */
  advertisesVideo: boolean
  endpoints: { httpPort: number | null }
}

export interface CameraState {
  batteryLevel: number | null
  /** 'idle' | 'shooting' | 'selfTimerCountdown', per the spec's captureStatus. */
  captureStatus: string
  remainingVideoSeconds: number | null
  remainingPictures: number | null
  storageBytesRemaining: number | null
}

export interface CameraFile {
  name: string
  /** Absolute URL on the camera. Used verbatim for download. */
  fileUrl: string
  sizeBytes: number
  /** ISO 8601 when the camera reported one; cameras vary, so it may be null. */
  capturedAt: string | null
  isVideo: boolean
}

export class CameraError extends Error {
  constructor(
    message: string,
    readonly kind:
      | 'unreachable'
      | 'timeout'
      | 'protocol'
      | 'device-refused'
      | 'unsupported'
      | 'cancelled',
    readonly detail?: unknown,
  ) {
    super(message)
    this.name = 'CameraError'
  }
}

/**
 * What the capture flow needs from a camera, independent of protocol.
 *
 * A camera that does not speak OSC implements this interface instead of being special-cased in
 * the UI. That is the same rule the backend's `CameraProvider` follows, for the same reason.
 */
export interface CameraClient {
  readonly baseUrl: string
  probe(): Promise<{ info: CameraInfo; state: CameraState }>
  getState(): Promise<CameraState>
  startVideo(): Promise<void>
  stopVideo(): Promise<CameraFile[]>
  listFiles(limit?: number): Promise<CameraFile[]>
  download(
    file: CameraFile,
    destination: string,
    onProgress?: (fraction: number, bytesWritten: number) => void,
  ): Promise<string>
}

interface FetchLike {
  (input: string, init?: Record<string, unknown>): Promise<{
    ok: boolean
    status: number
    json(): Promise<unknown>
    text(): Promise<string>
  }>
}

/** Injected so the protocol can be tested without a camera or a filesystem. */
export interface CameraTransport {
  fetch: FetchLike
  downloadFile(
    url: string,
    destination: string,
    onProgress?: (fraction: number, bytesWritten: number) => void,
  ): Promise<string>
}

async function withTimeout<T>(
  operation: (signal: AbortSignal) => Promise<T>,
  milliseconds: number,
  what: string,
): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), milliseconds)
  try {
    return await operation(controller.signal)
  } catch (error) {
    if (controller.signal.aborted) {
      throw new CameraError(
        `${what} timed out after ${Math.round(milliseconds / 1000)}s — is the phone still on the camera's Wi-Fi?`,
        'timeout',
      )
    }
    throw error
  } finally {
    clearTimeout(timer)
  }
}

export class OscCameraClient implements CameraClient {
  constructor(
    readonly baseUrl: string,
    private readonly transport: CameraTransport,
  ) {}

  private async post(path: string, body: unknown, timeoutMs: number): Promise<unknown> {
    const url = `${this.baseUrl.replace(/\/$/, '')}${path}`
    const response = await withTimeout(
      (signal) =>
        this.transport.fetch(url, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json;charset=utf-8' },
          body: JSON.stringify(body),
          signal,
        }),
      timeoutMs,
      `request to ${path}`,
    ).catch((error: unknown) => {
      if (error instanceof CameraError) throw error
      // A failed fetch on a private network almost always means "not joined", and that is the
      // one thing the operator can actually fix, so it is said plainly rather than as a stack.
      throw new CameraError(
        `Could not reach a camera at ${this.baseUrl}. Join the camera's Wi-Fi network and try again.`,
        'unreachable',
        error,
      )
    })

    if (!response.ok) {
      throw new CameraError(
        `The camera refused the request (HTTP ${response.status}).`,
        'device-refused',
        await response.text().catch(() => undefined),
      )
    }

    try {
      return await response.json()
    } catch (error) {
      throw new CameraError('The camera sent a response that was not JSON.', 'protocol', error)
    }
  }

  /** `POST /osc/commands/execute`, the spec's single command entry point. */
  private async execute(name: string, parameters?: Record<string, unknown>): Promise<unknown> {
    const payload = await this.post(
      '/osc/commands/execute',
      parameters ? { name, parameters } : { name },
      COMMAND_TIMEOUT_MS,
    )
    const envelope = payload as { state?: string; error?: { code?: string; message?: string } }
    if (envelope?.error) {
      throw new CameraError(
        envelope.error.message ?? `The camera rejected ${name}.`,
        'device-refused',
        envelope.error,
      )
    }
    return payload
  }

  async probe(): Promise<{ info: CameraInfo; state: CameraState }> {
    const raw = await withTimeout(
      (signal) => this.transport.fetch(`${this.baseUrl.replace(/\/$/, '')}/osc/info`, { signal }),
      REQUEST_TIMEOUT_MS,
      'camera info request',
    ).catch((error: unknown) => {
      if (error instanceof CameraError) throw error
      throw new CameraError(
        `No camera answered at ${this.baseUrl}.`,
        'unreachable',
        error,
      )
    })

    if (!raw.ok) {
      throw new CameraError(
        `The device at ${this.baseUrl} answered HTTP ${raw.status} — it may not be an OSC camera.`,
        'device-refused',
      )
    }

    const body = (await raw.json().catch(() => {
      throw new CameraError(
        `The device at ${this.baseUrl} is not an OSC camera (its /osc/info is not JSON).`,
        'protocol',
      )
    })) as Record<string, unknown>

    const apiLevel = Array.isArray(body.apiLevel)
      ? body.apiLevel.filter((value): value is number => typeof value === 'number')
      : []

    const info: CameraInfo = {
      manufacturer: typeof body.manufacturer === 'string' ? body.manufacturer : 'unknown',
      model: typeof body.model === 'string' ? body.model : 'unknown',
      serialNumber: typeof body.serialNumber === 'string' ? body.serialNumber : null,
      firmwareVersion: typeof body.firmwareVersion === 'string' ? body.firmwareVersion : null,
      apiLevel,
      // Straight from the declared API level. See the field's doc comment: this is what the
      // device claims, not a verdict on what it can do.
      advertisesVideo: apiLevel.includes(2),
      endpoints: {
        httpPort:
          typeof body.endpoints === 'object' && body.endpoints !== null
            ? (((body.endpoints as Record<string, unknown>).httpPort as number) ?? null)
            : null,
      },
    }

    return { info, state: await this.getState() }
  }

  async getState(): Promise<CameraState> {
    const payload = (await this.post('/osc/state', {}, REQUEST_TIMEOUT_MS)) as {
      state?: Record<string, unknown>
    }
    const state = payload?.state ?? {}
    return {
      batteryLevel: numberOrNull(state.batteryLevel),
      captureStatus:
        typeof state._captureStatus === 'string'
          ? state._captureStatus
          : typeof state.captureStatus === 'string'
            ? state.captureStatus
            : 'idle',
      remainingVideoSeconds: numberOrNull(state.remainingVideoSeconds),
      remainingPictures: numberOrNull(state.remainingPictures),
      storageBytesRemaining: numberOrNull(state.remainingSpace),
    }
  }

  async startVideo(): Promise<void> {
    // Two steps per the spec: put the camera in video mode, then start. Setting the mode every
    // time is deliberate — the operator may have left the camera in stills mode, and a failed
    // start halfway through a walkthrough is far worse than one redundant call.
    await this.execute('camera.setOptions', { options: { captureMode: 'video' } })
    await this.execute('camera.startCapture')
  }

  async stopVideo(): Promise<CameraFile[]> {
    const payload = (await this.execute('camera.stopCapture')) as {
      results?: { fileUrls?: unknown }
    }
    const urls = payload?.results?.fileUrls
    if (!Array.isArray(urls) || urls.length === 0) {
      // Not an error. Several cameras finalise the file asynchronously and return nothing here,
      // so the caller falls back to listing recent files.
      return []
    }
    return urls
      .filter((url): url is string => typeof url === 'string')
      .map((url) => ({
        name: url.split('/').pop() ?? 'capture.mp4',
        fileUrl: url,
        sizeBytes: 0, // Unknown until listed or downloaded; never invented.
        capturedAt: null,
        isVideo: true,
      }))
  }

  async listFiles(limit = 20): Promise<CameraFile[]> {
    const payload = (await this.execute('camera.listFiles', {
      fileType: 'all',
      entryCount: limit,
      maxThumbSize: 0,
    })) as { results?: { entries?: unknown } }

    const entries = payload?.results?.entries
    if (!Array.isArray(entries)) {
      throw new CameraError('The camera did not return a file list.', 'protocol', payload)
    }

    return entries
      .filter((entry): entry is Record<string, unknown> => typeof entry === 'object' && entry !== null)
      .map((entry) => {
        const name = typeof entry.name === 'string' ? entry.name : 'capture'
        const fileUrl = typeof entry.fileUrl === 'string' ? entry.fileUrl : ''
        return {
          name,
          fileUrl,
          sizeBytes: numberOrNull(entry.size) ?? 0,
          capturedAt: typeof entry.dateTimeZone === 'string' ? entry.dateTimeZone : null,
          isVideo: /\.(mp4|mov|insv)$/i.test(name),
        }
      })
      .filter((file) => file.fileUrl.length > 0)
  }

  async download(
    file: CameraFile,
    destination: string,
    onProgress?: (fraction: number, bytesWritten: number) => void,
  ): Promise<string> {
    if (!file.fileUrl) {
      throw new CameraError(`${file.name} has no URL on the camera.`, 'protocol')
    }
    return withTimeout(
      () => this.transport.downloadFile(file.fileUrl, destination, onProgress),
      DOWNLOAD_TIMEOUT_MS,
      `download of ${file.name}`,
    )
  }
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/**
 * Try each known camera address and return the first that answers.
 *
 * Sequential, not parallel: the phone is on a tiny private network, the list is three entries,
 * and a burst of simultaneous requests to a camera that is mid-boot is a good way to get all
 * three refused.
 */
export async function discoverCamera(
  transport: CameraTransport,
  hosts: readonly { host: string }[] = KNOWN_CAMERA_HOSTS,
): Promise<{ client: OscCameraClient; info: CameraInfo; state: CameraState }> {
  const failures: string[] = []
  for (const { host } of hosts) {
    const client = new OscCameraClient(host, transport)
    try {
      const { info, state } = await client.probe()
      return { client, info, state }
    } catch (error) {
      failures.push(`${host}: ${error instanceof Error ? error.message : String(error)}`)
    }
  }
  throw new CameraError(
    'No 360 camera answered on this network. Check that the phone is joined to the camera’s Wi-Fi, then try again.',
    'unreachable',
    failures,
  )
}
