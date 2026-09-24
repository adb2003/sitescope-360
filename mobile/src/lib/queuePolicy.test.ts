/**
 * Keeping the upload queue recoverable.
 *
 * Two rules, both about a capture that would otherwise be stuck on a device with no way off it —
 * which on this product is the failure that matters most, because until a file uploads the phone
 * holds the only copy of a walk somebody physically took.
 */

import { describe, expect, it } from 'vitest'

import { isRemovable, stuckUploadIds } from './queuePolicy'

describe('isRemovable', () => {
  it('allows removing a file that has not uploaded', () => {
    // The wrong floor, the wrong take, a 2 GiB video picked by mistake. Before this there was no
    // way to take one out: it held device storage, and because files upload serially and a
    // failure stops the run, one that kept failing blocked every capture queued behind it.
    expect(isRemovable('pending')).toBe(true)
    expect(isRemovable('failed')).toBe(true)
  })

  it('allows removing one stuck mid-upload', () => {
    // A row can sit in `uploading` indefinitely when the process died during the transfer. A
    // status that blocked its own cleanup is how a file becomes permanently unremovable.
    expect(isRemovable('uploading')).toBe(true)
  })

  it('refuses to remove an uploaded file', () => {
    // It is the server's now, and the record of what was sent is the only way to answer "did
    // that upload?" from the phone. `discardUploaded` reclaims the space without losing that.
    expect(isRemovable('uploaded')).toBe(false)
  })
})

describe('stuckUploadIds', () => {
  it('finds rows left claiming to be uploading', () => {
    // At the moment the database opens, nothing can legitimately be uploading — whatever process
    // was doing it is gone. Left alone the row is invisible to `sync`, which picks up only
    // `pending` and `failed`, so the capture silently never uploads and nothing explains why.
    const rows = [
      { id: 'a', status: 'pending' },
      { id: 'b', status: 'uploading' },
      { id: 'c', status: 'uploaded' },
      { id: 'd', status: 'uploading' },
    ]

    expect(stuckUploadIds(rows)).toEqual(['b', 'd'])
  })

  it('leaves every other status alone', () => {
    const rows = [
      { id: 'a', status: 'pending' },
      { id: 'b', status: 'failed' },
      { id: 'c', status: 'uploaded' },
    ]

    expect(stuckUploadIds(rows)).toEqual([])
  })

  it('handles an empty queue', () => {
    expect(stuckUploadIds([])).toEqual([])
  })
})
