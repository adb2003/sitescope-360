/**
 * Tests for the capture sequence, with the camera and the upload queue both faked.
 *
 * The branch worth testing is the one that is easy to get wrong and expensive when it is: a
 * camera that returns no filename from `stopCapture` because it is still writing. Treating that
 * as a failure would strand a recording that succeeded, which on a construction site means an
 * operator walking a floor twice.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { CameraError, type CameraClient, type CameraFile } from './osc'

vi.mock('./cameraTransport', () => ({ CAMERA_DOWNLOAD_DIR: '/camera/' }))

// vi.hoisted, because vi.mock's factory is hoisted above every top-level binding: a factory that
// closes over a plain const throws "cannot access before initialization" at collection time.
const { enqueueFile } = vi.hoisted(() => ({
  enqueueFile: vi.fn(
    async (
      sessionId: string,
      file: { uri: string; name: string; size?: number; mimeType?: string },
    ) => ({
      id: 'queued-1',
      sessionId,
      localUri: file.uri,
      name: file.name,
      sizeBytes: 1,
      mimeType: 'video/mp4',
      status: 'pending' as const,
      attempts: 0,
      lastError: null,
      queuedAt: '2026-09-11T00:00:00.000Z',
    }),
  ),
}))

vi.mock('./queue', () => ({ enqueueFile }))

import {
  snapshotFileUrls,
  startRecording,
  stopRecording,
  transferToQueue,
} from './cameraCapture'

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

function video(name: string, url = `http://cam/${name}`): CameraFile {
  return { name, fileUrl: url, sizeBytes: 1024, capturedAt: null, isVideo: true }
}

function fakeCamera(overrides: Partial<CameraClient> = {}): CameraClient {
  return {
    baseUrl: 'http://cam',
    probe: vi.fn(),
    getState: vi.fn(),
    startVideo: vi.fn(async () => undefined),
    stopVideo: vi.fn(async () => []),
    listFiles: vi.fn(async () => []),
    download: vi.fn(async (_file, destination) => destination),
    ...overrides,
  } as CameraClient
}

beforeEach(() => {
  enqueueFile.mockClear()
  vi.useRealTimers()
})

describe('stopRecording', () => {
  it('uses the filename the camera reports', async () => {
    const camera = fakeCamera({ stopVideo: vi.fn(async () => [video('R001.MP4')]) })

    const files = await stopRecording(camera)

    expect(files.map((file) => file.name)).toEqual(['R001.MP4'])
    // No need to poll when the camera already answered.
    expect(camera.listFiles).not.toHaveBeenCalled()
  })

  it('finds the file by polling when the camera reports none', async () => {
    // The case that must not be treated as failure: the recording exists, the camera is just
    // still writing it.
    let calls = 0
    const camera = fakeCamera({
      stopVideo: vi.fn(async () => []),
      listFiles: vi.fn(async () => {
        calls += 1
        return calls < 2 ? [video('OLD.MP4')] : [video('OLD.MP4'), video('NEW.MP4')]
      }),
    })

    const files = await stopRecording(camera, ['http://cam/OLD.MP4'])

    expect(files.map((file) => file.name)).toEqual(['NEW.MP4'])
  }, 40_000)

  it('ignores files that were already on the camera', async () => {
    const camera = fakeCamera({
      stopVideo: vi.fn(async () => []),
      listFiles: vi.fn(async () => [video('OLD.MP4')]),
    })

    // Only pre-existing files, so nothing new was produced -- and it must say so rather than
    // queueing last week's walkthrough as today's.
    await expect(stopRecording(camera, ['http://cam/OLD.MP4'])).rejects.toBeInstanceOf(CameraError)
  }, 45_000)

  it('tells the operator the recording is still on the camera when it gives up', async () => {
    const camera = fakeCamera({ stopVideo: vi.fn(async () => []) })

    const failure = await expectCameraError(stopRecording(camera))

    expect(failure.kind).toBe('timeout')
    expect(failure.message).toMatch(/imported from the camera tab/i)
  }, 45_000)

  it('survives a camera that errors while listing', async () => {
    const camera = fakeCamera({
      stopVideo: vi.fn(async () => []),
      listFiles: vi.fn(async () => {
        throw new Error('busy')
      }),
    })

    // A refused list is not a lost recording; it times out with the reassuring message.
    await expect(stopRecording(camera)).rejects.toMatchObject({ kind: 'timeout' })
  }, 45_000)
})

describe('transferToQueue', () => {
  it('queues the downloaded file under the session', async () => {
    const camera = fakeCamera()

    const queued = await transferToQueue(camera, 'CAP-20260911-103022-A8F3', video('R001.MP4'))

    expect(queued.sessionId).toBe('CAP-20260911-103022-A8F3')
    expect(enqueueFile).toHaveBeenCalledOnce()
    expect(enqueueFile.mock.calls[0]?.[1]).toMatchObject({
      name: 'R001.MP4',
      mimeType: 'video/mp4',
    })
  })

  it('sanitises the filename before using it as a path', async () => {
    const camera = fakeCamera()

    await transferToQueue(camera, 'session', video('../../etc/passwd.MP4'))

    const destination = (camera.download as ReturnType<typeof vi.fn>).mock.calls[0]?.[1] as string
    expect(destination).not.toContain('..')
    expect(destination.startsWith('/camera/')).toBe(true)
  })

  it('does not pass on a size the camera never reported', async () => {
    const camera = fakeCamera()
    const unsized: CameraFile = { ...video('R002.MP4'), sizeBytes: 0 }

    await transferToQueue(camera, 'session', unsized)

    // undefined, not 0: the queue measures the file itself rather than recording a false size.
    expect(enqueueFile.mock.calls[0]?.[1]).toMatchObject({ size: undefined })
  })

  it('reports progress so a long transfer does not look like a hang', async () => {
    const seen: number[] = []
    const camera = fakeCamera({
      download: vi.fn(async (_file, destination, onProgress) => {
        onProgress?.(0.5, 512)
        onProgress?.(1, 1024)
        return destination as string
      }),
    })

    await transferToQueue(camera, 'session', video('R003.MP4'), (progress) =>
      seen.push(progress.fraction),
    )

    expect(seen).toEqual([0.5, 1])
  })
})

describe('startRecording and snapshotFileUrls', () => {
  it('starts video on the camera', async () => {
    const camera = fakeCamera()
    await startRecording(camera)
    expect(camera.startVideo).toHaveBeenCalledOnce()
  })

  it('returns an empty snapshot rather than failing when the camera refuses to list', async () => {
    const camera = fakeCamera({
      listFiles: vi.fn(async () => {
        throw new Error('busy')
      }),
    })

    // The snapshot is an optimisation for spotting new files; it must never block a capture.
    await expect(snapshotFileUrls(camera)).resolves.toEqual([])
  })
})

describe('filename sanitisation', () => {
  it.each([
    ['../../etc/passwd.MP4', 'no traversal segments'],
    ['....//....//x.MP4', 'no collapsed traversal'],
    ['/absolute.MP4', 'not absolute'],
  ])('makes %s safe (%s)', async (name) => {
    const camera = fakeCamera()
    await transferToQueue(camera, 'session', video(name))

    const destination = (camera.download as ReturnType<typeof vi.fn>).mock.calls.at(-1)
      ?.[1] as string
    expect(destination).not.toContain('..')
    expect(destination.startsWith('/camera/')).toBe(true)
    // Exactly one path segment after the directory.
    expect(destination.slice('/camera/'.length)).not.toContain('/')
  })

  it('falls back to a name when sanitising leaves nothing', async () => {
    const camera = fakeCamera()
    await transferToQueue(camera, 'session', video('...'))

    const destination = (camera.download as ReturnType<typeof vi.fn>).mock.calls.at(-1)
      ?.[1] as string
    expect(destination).toMatch(/capture$/)
  })
})
