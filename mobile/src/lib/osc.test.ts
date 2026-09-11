/**
 * Tests for the OSC camera client.
 *
 * There is no 360 camera on this project, so the protocol is exercised against a fake transport.
 * That covers what is actually risky here: envelope parsing, the spec's error shape, missing and
 * malformed fields, timeouts, and discovery across candidate addresses.
 *
 * What it cannot cover is whether a physical device behaves as the specification says. That is
 * stated in `osc.ts` and in the docs rather than implied away — these tests prove the client is
 * correct about OSC, not that any particular camera is.
 */

import { describe, expect, it, vi } from 'vitest'

import {
  CameraError,
  KNOWN_CAMERA_HOSTS,
  OscCameraClient,
  discoverCamera,
  type CameraTransport,
} from './osc'

/** Narrows a rejection to a CameraError, failing the test with the real value if it is not one. */
async function expectCameraError(promise: Promise<unknown>): Promise<CameraError> {
  try {
    const resolved = await promise
    throw new Error(`expected a CameraError but it resolved with ${JSON.stringify(resolved)}`)
  } catch (caught) {
    if (caught instanceof CameraError) return caught
    throw caught
  }
}

function response(body: unknown, ok = true, status = 200) {
  return {
    ok,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  }
}

function transport(
  handler: (url: string, init?: Record<string, unknown>) => Promise<ReturnType<typeof response>>,
): CameraTransport {
  return {
    fetch: handler as CameraTransport['fetch'],
    downloadFile: vi.fn(async (_url, destination) => destination),
  }
}

const INFO = {
  manufacturer: 'RICOH',
  model: 'RICOH THETA Z1',
  serialNumber: '10010293',
  firmwareVersion: '2.10.3',
  apiLevel: [2],
  endpoints: { httpPort: 80 },
}

const STATE = {
  state: {
    batteryLevel: 0.82,
    _captureStatus: 'idle',
    remainingVideoSeconds: 1200,
    remainingPictures: 400,
    remainingSpace: 12_000_000_000,
  },
}

describe('probe', () => {
  it('reports what the device actually said', async () => {
    const client = new OscCameraClient(
      'http://192.168.1.1',
      transport(async (url) =>
        url.endsWith('/osc/info') ? response(INFO) : response(STATE),
      ),
    )

    const { info, state } = await client.probe()

    expect(info.manufacturer).toBe('RICOH')
    expect(info.model).toBe('RICOH THETA Z1')
    expect(info.apiLevel).toEqual([2])
    expect(info.advertisesVideo).toBe(true)
    expect(state.batteryLevel).toBeCloseTo(0.82)
    expect(state.captureStatus).toBe('idle')
  })

  it('does not invent fields the camera omitted', async () => {
    const client = new OscCameraClient(
      'http://192.168.1.1',
      transport(async (url) =>
        url.endsWith('/osc/info')
          ? response({ model: 'Mystery 360' })
          : response({ state: {} }),
      ),
    )

    const { info, state } = await client.probe()

    expect(info.manufacturer).toBe('unknown')
    expect(info.serialNumber).toBeNull()
    expect(info.firmwareVersion).toBeNull()
    expect(info.apiLevel).toEqual([])
    // No declared level 2, so no claim of video support -- see the field's doc comment.
    expect(info.advertisesVideo).toBe(false)
    expect(state.batteryLevel).toBeNull()
    expect(state.storageBytesRemaining).toBeNull()
    // An absent captureStatus means the camera is not recording, which is the safe reading.
    expect(state.captureStatus).toBe('idle')
  })

  it('says "not a camera" rather than "broken" when something else answers', async () => {
    const client = new OscCameraClient(
      'http://192.168.1.1',
      transport(async () => ({
        ok: true,
        status: 200,
        json: async () => {
          throw new Error('Unexpected token < in JSON')
        },
        text: async () => '<html>router admin</html>',
      })),
    )

    // A home router at the same address is the realistic case, and the message should point the
    // operator at the actual problem.
    await expect(client.probe()).rejects.toThrow(/not an OSC camera/i)
  })

  it('surfaces an unreachable network as advice, not a stack trace', async () => {
    const client = new OscCameraClient(
      'http://192.168.1.1',
      transport(async () => {
        throw new TypeError('Network request failed')
      }),
    )

    await expect(client.probe()).rejects.toMatchObject({ kind: 'unreachable' })
  })
})

describe('recording', () => {
  it('sets video mode before starting, so a camera left in stills mode still works', async () => {
    const commands: string[] = []
    const client = new OscCameraClient(
      'http://192.168.1.1',
      transport(async (_url, init) => {
        const body = JSON.parse(String(init?.body ?? '{}'))
        commands.push(body.name)
        return response({ state: 'done' })
      }),
    )

    await client.startVideo()

    expect(commands).toEqual(['camera.setOptions', 'camera.startCapture'])
  })

  it('returns the finished file when the camera names one', async () => {
    const client = new OscCameraClient(
      'http://192.168.1.1',
      transport(async () =>
        response({
          results: { fileUrls: ['http://192.168.1.1/files/100RICOH/R0010081.MP4'] },
        }),
      ),
    )

    const files = await client.stopVideo()

    expect(files).toHaveLength(1)
    expect(files[0]?.name).toBe('R0010081.MP4')
    expect(files[0]?.isVideo).toBe(true)
    // Size is unknown at this point and must not be guessed.
    expect(files[0]?.sizeBytes).toBe(0)
  })

  it('treats an empty stop response as "finalising", not as failure', async () => {
    // Several cameras finalise asynchronously and name no file here. Throwing would strand a
    // recording that actually succeeded, so the caller falls back to listing recent files.
    const client = new OscCameraClient(
      'http://192.168.1.1',
      transport(async () => response({ results: {} })),
    )

    await expect(client.stopVideo()).resolves.toEqual([])
  })

  it('raises the camera’s own message when it rejects a command', async () => {
    const client = new OscCameraClient(
      'http://192.168.1.1',
      transport(async () =>
        response({
          error: { code: 'disabledCommand', message: 'Command is currently disabled' },
        }),
      ),
    )

    await expect(client.startVideo()).rejects.toThrow('Command is currently disabled')
    await expect(client.startVideo()).rejects.toMatchObject({ kind: 'device-refused' })
  })
})

describe('listFiles', () => {
  it('parses entries and classifies video by extension', async () => {
    const client = new OscCameraClient(
      'http://192.168.1.1',
      transport(async () =>
        response({
          results: {
            entries: [
              {
                name: 'R0010081.MP4',
                fileUrl: 'http://192.168.1.1/files/R0010081.MP4',
                size: 524_288_000,
                dateTimeZone: '2026-09-11 10:30:22+05:30',
              },
              {
                name: 'R0010082.JPG',
                fileUrl: 'http://192.168.1.1/files/R0010082.JPG',
                size: 8_400_000,
              },
            ],
          },
        }),
      ),
    )

    const files = await client.listFiles()

    expect(files.map((file) => file.isVideo)).toEqual([true, false])
    expect(files[0]?.sizeBytes).toBe(524_288_000)
    expect(files[0]?.capturedAt).toBe('2026-09-11 10:30:22+05:30')
    expect(files[1]?.capturedAt).toBeNull()
  })

  it('drops entries with no URL instead of yielding undownloadable files', async () => {
    const client = new OscCameraClient(
      'http://192.168.1.1',
      transport(async () =>
        response({ results: { entries: [{ name: 'ghost.mp4' }, 'nonsense', null] } }),
      ),
    )

    await expect(client.listFiles()).resolves.toEqual([])
  })

  it('fails loudly when the response has no entries at all', async () => {
    const client = new OscCameraClient(
      'http://192.168.1.1',
      transport(async () => response({ results: {} })),
    )

    // Distinct from "no files": a malformed response must not read as an empty camera.
    await expect(client.listFiles()).rejects.toMatchObject({ kind: 'protocol' })
  })
})

describe('download', () => {
  it('passes the camera’s URL through verbatim', async () => {
    const base = transport(async () => response({}))
    const client = new OscCameraClient('http://192.168.1.1', base)
    const file = {
      name: 'R0010081.MP4',
      fileUrl: 'http://192.168.1.1/files/100RICOH/R0010081.MP4',
      sizeBytes: 1,
      capturedAt: null,
      isVideo: true,
    }

    await client.download(file, '/tmp/out.mp4')

    expect(base.downloadFile).toHaveBeenCalledWith(file.fileUrl, '/tmp/out.mp4', undefined)
  })

  it('refuses a file with no URL', async () => {
    const client = new OscCameraClient('http://192.168.1.1', transport(async () => response({})))
    await expect(
      client.download(
        { name: 'x', fileUrl: '', sizeBytes: 0, capturedAt: null, isVideo: true },
        '/tmp/x',
      ),
    ).rejects.toBeInstanceOf(CameraError)
  })
})

describe('discoverCamera', () => {
  it('returns the first address that answers', async () => {
    const tried: string[] = []
    const found = await discoverCamera(
      transport(async (url) => {
        tried.push(url)
        if (!url.startsWith('http://192.168.42.1')) {
          throw new TypeError('Network request failed')
        }
        return url.endsWith('/osc/info') ? response(INFO) : response(STATE)
      }),
    )

    expect(found.client.baseUrl).toBe('http://192.168.42.1')
    // Sequential, not parallel: the first host is tried and rejected before the second.
    expect(tried[0]).toContain('192.168.1.1')
  })

  it('reports every address it tried when none answer', async () => {
    const error = await expectCameraError(
      discoverCamera(
        transport(async () => {
          throw new TypeError('Network request failed')
        }),
      ),
    )

    expect(error.kind).toBe('unreachable')
    expect(error.message).toMatch(/Wi-?Fi/i)
    expect(Array.isArray(error.detail)).toBe(true)
    expect(error.detail).toHaveLength(KNOWN_CAMERA_HOSTS.length)
  })
})

describe('timeouts', () => {
  it('reports a hung socket as a timeout that names the likely cause', async () => {
    const client = new OscCameraClient(
      'http://192.168.1.1',
      transport(
        (_url, init) =>
          new Promise((_resolve, reject) => {
            const signal = init?.signal as AbortSignal | undefined
            signal?.addEventListener('abort', () => reject(new Error('aborted')))
          }) as never,
      ),
    )

    const failure = await expectCameraError(client.getState())

    expect(failure.kind).toBe('timeout')
    expect(failure.message).toMatch(/Wi-?Fi/i)
  }, 20_000)
})
