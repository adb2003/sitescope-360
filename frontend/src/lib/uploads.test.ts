/**
 * The chunked upload, tested where it can be wrong without looking wrong.
 *
 * Every mistake available here produces a finished upload. Slice the file from the wrong offset
 * and the object is the right length and the wrong bytes. Use the part's own number instead of
 * the plan's part size and the slices overlap. Drop the resume handle on failure and the retry
 * succeeds, having re-sent everything — the one cost the route exists to remove. None of that
 * shows up in a screenshot or a status code, so it is pinned here instead.
 *
 * `api` and `putSignedUpload` are mocked; what is under test is the arithmetic between them and
 * the order the three calls happen in.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/api', async () => {
  const actual = await vi.importActual<typeof import('@/lib/api')>('@/lib/api')
  return {
    ...actual,
    api: { ...actual.api, post: vi.fn() },
    putSignedUpload: vi.fn(),
  }
})

import { ApiError, api, putSignedUpload } from '@/lib/api'
import { ResumableUploadInterrupted, uploadResumable } from '@/lib/uploads'
import type { MultipartTicket } from '@/types/api'

const post = vi.mocked(api.post)
const put = vi.mocked(putSignedUpload)

const PART = 8 * 1024 * 1024
const SIZE = PART * 2 + 512

/** A file whose every byte encodes its own offset, so a misplaced slice cannot go unnoticed. */
function markedFile(size = SIZE): File {
  const bytes = new Uint8Array(size)
  for (let i = 0; i < size; i += 1) bytes[i] = i % 251
  return new File([bytes], 'walk.mp4', { type: 'video/mp4' })
}

function ticket(parts: number[], overrides: Partial<MultipartTicket> = {}): MultipartTicket {
  const total = overrides.total_bytes ?? SIZE
  const count = overrides.part_count ?? 3
  return {
    storage_key: 'projects/p/floors/f/sessions/s/source/abc.mp4',
    upload_id: 'upload-1',
    part_size: PART,
    part_count: count,
    total_bytes: total,
    received: Array.from({ length: count }, (_, i) => i + 1).filter((n) => !parts.includes(n)),
    parts: parts.map((number) => ({
      part_number: number,
      upload_url: `https://storage.test/part/${number}`,
      method: 'PUT',
      headers: {},
      size_bytes: number === count ? total - PART * (count - 1) : PART,
    })),
    expires_in: 3600,
    ...overrides,
  }
}

/**
 * The first byte of a slice, read the long way round.
 *
 * `Blob.arrayBuffer` is absent from jsdom's implementation, so a `FileReader` does the job. Worth
 * the ceremony: this is the only way to see *which* bytes were sent rather than how many.
 */
function firstByte(blob: Blob): Promise<number> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(new Uint8Array(reader.result as ArrayBuffer)[0] ?? -1)
    reader.onerror = () => reject(reader.error)
    reader.readAsArrayBuffer(blob)
  })
}

const CREATED = { id: 'file-1' }

beforeEach(() => {
  post.mockReset()
  put.mockReset()
  put.mockResolvedValue(undefined)
})

afterEach(() => vi.clearAllMocks())

describe('uploadResumable', () => {
  it('sends every part exactly once, in order, covering the file with no gap or overlap', async () => {
    post.mockResolvedValueOnce(ticket([1, 2, 3]) as never).mockResolvedValueOnce(CREATED as never)
    const file = markedFile()

    await uploadResumable('CAP-1', file)

    expect(put).toHaveBeenCalledTimes(3)
    const slices = put.mock.calls.map(([url, blob]) => ({ url, size: blob.size }))
    expect(slices.map((s) => s.url)).toEqual([
      'https://storage.test/part/1',
      'https://storage.test/part/2',
      'https://storage.test/part/3',
    ])
    expect(slices.map((s) => s.size)).toEqual([PART, PART, 512])
    expect(slices.reduce((sum, s) => sum + s.size, 0)).toBe(file.size)
  })

  it('slices each part from the offset the server will assemble it at', async () => {
    // The check the file's contents were built for. An off-by-one-part offset produces an object
    // of exactly the right size, assembled without error, containing the wrong bytes.
    post.mockResolvedValueOnce(ticket([1, 2, 3]) as never).mockResolvedValueOnce(CREATED as never)

    await uploadResumable('CAP-1', markedFile())

    expect(await firstByte(put.mock.calls[0]![1] as Blob)).toBe(0)
    expect(await firstByte(put.mock.calls[1]![1] as Blob)).toBe(PART % 251)
    expect(await firstByte(put.mock.calls[2]![1] as Blob)).toBe((PART * 2) % 251)
  })

  it('reports progress that starts from what already landed, not from zero', async () => {
    // A resume of a nearly-finished upload must not show the bar jumping backwards; the
    // operator reads that as the transfer having been thrown away, which is the fear this whole
    // route is meant to answer.
    post.mockResolvedValueOnce(ticket([3]) as never).mockResolvedValueOnce(CREATED as never)
    const seen: number[] = []

    await uploadResumable('CAP-1', markedFile(), (transferred) => seen.push(transferred), {
      storage_key: 'projects/p/floors/f/sessions/s/source/abc.mp4',
      upload_id: 'upload-1',
    })

    expect(seen[0]).toBe(PART * 2)
    expect(seen[seen.length - 1]).toBe(SIZE)
    // Never backwards, and never past the end.
    expect(seen.every((value, i) => i === 0 || value >= seen[i - 1]!)).toBe(true)
    expect(Math.max(...seen)).toBeLessThanOrEqual(SIZE)
  })

  it('asks to resume rather than to start, when given a handle', async () => {
    post.mockResolvedValueOnce(ticket([2, 3]) as never).mockResolvedValueOnce(CREATED as never)
    const handle = { storage_key: 'projects/p/x.mp4', upload_id: 'upload-9' }

    await uploadResumable('CAP-1', markedFile(), undefined, handle)

    expect(post.mock.calls[0]![0]).toBe('/captures/CAP-1/multipart/resume')
    expect(post.mock.calls[0]![1]).toMatchObject(handle)
    // Only the parts the server said were missing. Re-sending part 1 would work, and would be
    // the entire cost this route exists to avoid.
    expect(put).toHaveBeenCalledTimes(2)
  })

  it('hands the resume handle back when a part fails', async () => {
    // Without this the parts already in storage are unreachable and still billable, and the
    // retry starts from the beginning — a route that costs two extra round trips and buys
    // nothing at all.
    post.mockResolvedValueOnce(ticket([1, 2, 3]) as never)
    put.mockRejectedValueOnce(new ApiError(0, { code: 'network_error', message: 'offline' }))

    const failure = await uploadResumable('CAP-1', markedFile()).catch((error) => error)

    expect(failure).toBeInstanceOf(ResumableUploadInterrupted)
    expect((failure as ResumableUploadInterrupted).resume).toEqual({
      storage_key: 'projects/p/floors/f/sessions/s/source/abc.mp4',
      upload_id: 'upload-1',
    })
    expect((failure as ResumableUploadInterrupted).cause).toBeInstanceOf(ApiError)
  })

  it('hands it back when completion fails too, not only when a part does', async () => {
    // The parts are all in storage at this point, so this is the case where resuming is
    // cheapest and losing the handle costs most.
    post
      .mockResolvedValueOnce(ticket([1, 2, 3]) as never)
      .mockRejectedValueOnce(new ApiError(503, { code: 'unavailable', message: 'busy' }))

    const failure = await uploadResumable('CAP-1', markedFile()).catch((error) => error)

    expect(failure).toBeInstanceOf(ResumableUploadInterrupted)
    expect((failure as ResumableUploadInterrupted).resume.upload_id).toBe('upload-1')
  })

  it('starts a fresh upload when the one it was resuming no longer exists', async () => {
    // An upload can be gone for good reasons: aborted, expired, or completed into an object the
    // server then rejected and deleted. Retrying the resume would fail identically for ever.
    post
      .mockRejectedValueOnce(new ApiError(404, { code: 'not_found', message: 'gone' }))
      .mockResolvedValueOnce(ticket([1, 2, 3]) as never)
      .mockResolvedValueOnce(CREATED as never)

    await uploadResumable('CAP-1', markedFile(), undefined, {
      storage_key: 'projects/p/x.mp4',
      upload_id: 'stale',
    })

    expect(post.mock.calls[0]![0]).toBe('/captures/CAP-1/multipart/resume')
    expect(post.mock.calls[1]![0]).toBe('/captures/CAP-1/multipart')
    expect(put).toHaveBeenCalledTimes(3)
  })

  it('does not paper over an error that is not a missing upload', async () => {
    // A 403 means this client may not upload here. Silently starting a new upload would turn a
    // permission problem into an endless retry loop against the same wall.
    post.mockRejectedValueOnce(new ApiError(403, { code: 'forbidden', message: 'no' }))

    const failure = await uploadResumable('CAP-1', markedFile(), undefined, {
      storage_key: 'projects/p/x.mp4',
      upload_id: 'u',
    }).catch((error) => error)

    expect(failure).toBeInstanceOf(ApiError)
    expect((failure as ApiError).status).toBe(403)
    expect(put).not.toHaveBeenCalled()
  })

  it('completes against the key and upload it actually used', async () => {
    post.mockResolvedValueOnce(ticket([1, 2, 3]) as never).mockResolvedValueOnce(CREATED as never)

    await uploadResumable('CAP-1', markedFile())

    expect(post.mock.calls[1]).toEqual([
      '/captures/CAP-1/multipart/complete',
      {
        filename: 'walk.mp4',
        size_bytes: SIZE,
        storage_key: 'projects/p/floors/f/sessions/s/source/abc.mp4',
        upload_id: 'upload-1',
      },
    ])
  })
})
