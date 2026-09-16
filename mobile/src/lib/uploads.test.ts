/**
 * Uploading from the phone, tested where it can be wrong without looking wrong.
 *
 * This is the client that most needed the chunked route and was the last to get it: a device on
 * a half-built floor, holding the only copy of a walkthrough, on a connection that drops. Until
 * now every file went through the API in one POST, so a two-gigabyte upload that failed at
 * ninety percent was re-sent from zero.
 *
 * Every mistake available here still produces a finished upload. Read the part number as 0-based
 * and the object assembles, at the right size, shifted by one part. Drop the resume handle on
 * failure and the retry succeeds, having re-sent everything — the one cost the whole route
 * exists to remove. Leave a part file behind and the device fills up. None of it shows on screen,
 * so it is pinned here.
 *
 * The I/O is injected (see `UploadIO`), so all of this runs under plain Node with no Expo, no
 * Metro and no device.
 */

import { describe, expect, it, vi } from 'vitest'

import {
  DIRECT_UPLOAD_MIN_BYTES,
  RESUMABLE_UPLOAD_MIN_BYTES,
  bytesAlreadySent,
  partOffset,
  routeFor,
} from './uploadPlan'
import {
  ResumableUploadInterrupted,
  uploadFile,
  type LocalFile,
  type MultipartTicket,
  type UploadIO,
} from './uploads'

// A part size the server would pick for a very large file, chosen here so that three parts put
// the test file above the resumable threshold — the route under test only applies above it.
const PART = 32 * 1024 * 1024
const SIZE = PART * 2 + 512

const FILE: LocalFile = {
  uri: 'file:///captures/walk.mp4',
  name: 'walk.mp4',
  mimeType: 'video/mp4',
  sizeBytes: SIZE,
}

function ticket(missing: number[]): MultipartTicket {
  const count = 3
  return {
    storage_key: 'projects/p/floors/f/sessions/s/source/abc.mp4',
    upload_id: 'upload-1',
    part_size: PART,
    part_count: count,
    total_bytes: SIZE,
    received: [1, 2, 3].filter((n) => !missing.includes(n)),
    parts: missing.map((number) => ({
      part_number: number,
      upload_url: `https://storage.test/part/${number}`,
      headers: {},
      size_bytes: number === count ? 512 : PART,
    })),
  }
}

interface Recorder extends UploadIO {
  readonly posts: { path: string; body: unknown }[]
  readonly puts: { url: string; uri: string }[]
  readonly slices: { position: number; length: number }[]
  readonly removed: string[]
}

function recorder(responses: unknown[], overrides: Partial<UploadIO> = {}): Recorder {
  const posts: { path: string; body: unknown }[] = []
  const puts: { url: string; uri: string }[] = []
  const slices: { position: number; length: number }[] = []
  const removed: string[] = []
  let next = 0

  const io: Recorder = {
    posts,
    puts,
    slices,
    removed,
    async post(path, body) {
      posts.push({ path, body })
      const response = responses[next]
      next += 1
      if (response instanceof Error) throw response
      return response as never
    },
    async postFile(path, file) {
      posts.push({ path, body: file })
      return { id: 'proxied' } as never
    },
    async putFile(url, uri) {
      puts.push({ url, uri })
    },
    async writeSlice(_uri, position, length) {
      slices.push({ position, length })
      return `file:///parts/part-${position}.bin`
    },
    async remove(uri) {
      removed.push(uri)
    },
    ...overrides,
  }
  return io
}

const httpError = (status: number) => Object.assign(new Error(`HTTP ${status}`), { status })

// ---- the arithmetic ---------------------------------------------------------
describe('routeFor', () => {
  it('keeps a panorama on the proxied route and sends a walkthrough in parts', () => {
    expect(routeFor(300 * 1024)).toBe('proxied')
    expect(routeFor(40 * 1024 * 1024)).toBe('direct')
    expect(routeFor(900 * 1024 * 1024)).toBe('resumable')
  })

  it('treats a file exactly at either threshold as taking the simpler route', () => {
    expect(routeFor(DIRECT_UPLOAD_MIN_BYTES)).toBe('proxied')
    expect(routeFor(DIRECT_UPLOAD_MIN_BYTES + 1)).toBe('direct')
    expect(routeFor(RESUMABLE_UPLOAD_MIN_BYTES)).toBe('direct')
    expect(routeFor(RESUMABLE_UPLOAD_MIN_BYTES + 1)).toBe('resumable')
  })

  it('matches the web app, which is meant to route the same file the same way', () => {
    // The two are separate packages with no shared constant, so this is the only thing standing
    // between them and a slow drift where a phone and a browser disagree about a 50 MB file.
    expect(DIRECT_UPLOAD_MIN_BYTES).toBe(32 * 1024 * 1024)
    expect(RESUMABLE_UPLOAD_MIN_BYTES).toBe(64 * 1024 * 1024)
    expect(RESUMABLE_UPLOAD_MIN_BYTES).toBeGreaterThan(DIRECT_UPLOAD_MIN_BYTES)
  })
})

describe('partOffset', () => {
  it('reads part numbers as 1-based, because S3 does', () => {
    expect(partOffset(1, PART)).toBe(0)
    expect(partOffset(2, PART)).toBe(PART)
    expect(partOffset(9, PART)).toBe(PART * 8)
  })

  it('refuses a part number that cannot be one', () => {
    // A 0 here would read the same bytes as part 1 and shift the whole object; better to fail
    // than to upload something plausible.
    expect(() => partOffset(0, PART)).toThrow()
    expect(() => partOffset(-1, PART)).toThrow()
    expect(() => partOffset(1.5, PART)).toThrow()
  })
})

describe('bytesAlreadySent', () => {
  it('counts the parts the server is already holding', () => {
    expect(bytesAlreadySent(3, 1, PART, SIZE)).toBe(PART * 2)
    expect(bytesAlreadySent(3, 3, PART, SIZE)).toBe(0)
  })

  it('never exceeds the file, even when the held parts include the last one', () => {
    expect(bytesAlreadySent(3, 0, PART, SIZE)).toBe(SIZE)
  })
})

// ---- the routing ------------------------------------------------------------
describe('uploadFile', () => {
  it('posts a small file through the API, as before', async () => {
    const io = recorder([])

    await uploadFile(io, 'CAP-1', { ...FILE, name: 'pano.jpg', sizeBytes: 300 * 1024 })

    expect(io.posts.map((p) => p.path)).toEqual(['/captures/CAP-1/upload'])
    expect(io.puts).toEqual([])
  })

  it('sends a mid-sized file straight to storage in one PUT', async () => {
    const io = recorder([
      { storage_key: 'k', upload_url: 'https://storage.test/put', headers: {} },
      { id: 'file-1' },
    ])

    await uploadFile(io, 'CAP-1', { ...FILE, sizeBytes: 40 * 1024 * 1024 })

    expect(io.posts.map((p) => p.path)).toEqual([
      '/captures/CAP-1/upload-url',
      '/captures/CAP-1/files',
    ])
    expect(io.puts).toEqual([{ url: 'https://storage.test/put', uri: FILE.uri }])
  })

  it('sends a walkthrough in parts, each sliced from the offset the server assembles it at', async () => {
    const io = recorder([ticket([1, 2, 3]), { id: 'file-1' }])

    await uploadFile(io, 'CAP-1', FILE)

    expect(io.slices).toEqual([
      { position: 0, length: PART },
      { position: PART, length: PART },
      { position: PART * 2, length: 512 },
    ])
    expect(io.puts.map((p) => p.url)).toEqual([
      'https://storage.test/part/1',
      'https://storage.test/part/2',
      'https://storage.test/part/3',
    ])
    expect(io.slices.reduce((sum, s) => sum + s.length, 0)).toBe(SIZE)
  })

  it('deletes each part file as soon as it is sent', async () => {
    // The device queued this capture because it is the only copy; leaving 8 MiB behind per part
    // fills up exactly the storage the capture needs.
    const io = recorder([ticket([1, 2, 3]), { id: 'file-1' }])

    await uploadFile(io, 'CAP-1', FILE)

    expect(io.removed).toHaveLength(3)
    expect(io.removed).toEqual(io.puts.map((p) => p.uri))
  })

  it('deletes the part file even when the PUT fails', async () => {
    const io = recorder([ticket([1, 2, 3])], {
      putFile: vi.fn().mockRejectedValue(httpError(0)),
    })

    await uploadFile(io, 'CAP-1', FILE).catch(() => undefined)

    expect(io.removed).toEqual(['file:///parts/part-0.bin'])
  })

  it('hands the resume handle back when a part fails', async () => {
    const io = recorder([ticket([1, 2, 3])], {
      putFile: vi.fn().mockRejectedValue(httpError(0)),
    })

    const failure = await uploadFile(io, 'CAP-1', FILE).catch((error) => error)

    expect(failure).toBeInstanceOf(ResumableUploadInterrupted)
    expect((failure as ResumableUploadInterrupted).resume).toEqual({
      storageKey: 'projects/p/floors/f/sessions/s/source/abc.mp4',
      uploadId: 'upload-1',
    })
  })

  it('resumes with only the parts the server says are missing', async () => {
    const io = recorder([ticket([3]), { id: 'file-1' }])

    await uploadFile(io, 'CAP-1', FILE, undefined, {
      storageKey: 'projects/p/x.mp4',
      uploadId: 'upload-9',
    })

    expect(io.posts[0]!.path).toBe('/captures/CAP-1/multipart/resume')
    expect(io.posts[0]!.body).toMatchObject({
      storage_key: 'projects/p/x.mp4',
      upload_id: 'upload-9',
    })
    // Part 3 only. Re-sending 1 and 2 would work, and is the whole cost being avoided.
    expect(io.slices).toEqual([{ position: PART * 2, length: 512 }])
  })

  it('reports progress starting from what already landed, never going backwards', async () => {
    const io = recorder([ticket([3]), { id: 'file-1' }])
    const seen: number[] = []

    await uploadFile(io, 'CAP-1', FILE, (transferred) => seen.push(transferred), {
      storageKey: 'projects/p/x.mp4',
      uploadId: 'upload-9',
    })

    expect(seen[0]).toBe(PART * 2)
    expect(seen[seen.length - 1]).toBe(SIZE)
    expect(seen.every((value, i) => i === 0 || value >= seen[i - 1]!)).toBe(true)
  })

  it('starts fresh when the upload it was resuming no longer exists', async () => {
    // Aborted, expired, or completed into an object the server rejected and deleted. Retrying
    // the resume would fail identically for ever, and the file is still on the device.
    const io = recorder([httpError(404), ticket([1, 2, 3]), { id: 'file-1' }])

    await uploadFile(io, 'CAP-1', FILE, undefined, {
      storageKey: 'projects/p/x.mp4',
      uploadId: 'stale',
    })

    expect(io.posts.map((p) => p.path)).toEqual([
      '/captures/CAP-1/multipart/resume',
      '/captures/CAP-1/multipart',
      '/captures/CAP-1/multipart/complete',
    ])
    expect(io.slices).toHaveLength(3)
  })

  it('does not paper over an error that is not a missing upload', async () => {
    // A 403 means this client may not upload here. Quietly starting a new upload would turn a
    // permission problem into an endless retry loop against the same wall.
    const io = recorder([httpError(403)])

    const failure = await uploadFile(io, 'CAP-1', FILE, undefined, {
      storageKey: 'projects/p/x.mp4',
      uploadId: 'u',
    }).catch((error) => error)

    expect(failure).not.toBeInstanceOf(ResumableUploadInterrupted)
    expect((failure as { status: number }).status).toBe(403)
    expect(io.slices).toEqual([])
  })

  it('completes against the key and upload it actually used', async () => {
    const io = recorder([ticket([1, 2, 3]), { id: 'file-1' }])

    await uploadFile(io, 'CAP-1', FILE)

    expect(io.posts[1]).toEqual({
      path: '/captures/CAP-1/multipart/complete',
      body: {
        filename: 'walk.mp4',
        size_bytes: SIZE,
        storage_key: 'projects/p/floors/f/sessions/s/source/abc.mp4',
        upload_id: 'upload-1',
      },
    })
  })
})
