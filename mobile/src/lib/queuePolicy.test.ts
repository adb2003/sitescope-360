/**
 * Keeping the upload queue recoverable.
 *
 * Two rules, both about a capture that would otherwise be stuck on a device with no way off it —
 * which on this product is the failure that matters most, because until a file uploads the phone
 * holds the only copy of a walk somebody physically took.
 */

import { describe, expect, it } from 'vitest'

import {
  afterUploadFailure,
  isPermanentRefusal,
  isRemovable,
  statusOf,
  stuckUploadIds,
} from './queuePolicy'

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

describe('isPermanentRefusal', () => {
  /*
    The queue drains oldest first and stops on the first failure. That is right for "no signal"
    and wrong once the server has decided: a file it will never accept is the oldest failed row
    on every drain from then on, so it is tried first, fails, and stops the run — and every
    capture behind it stays on the phone, untried, for ever.

    Per-project roles made the 403 case ordinary: an engineer who is a viewer on *this* job
    cannot upload to it, and no amount of signal changes that.
  */
  it('treats a refusal the server will repeat as permanent', () => {
    expect(isPermanentRefusal(403)).toBe(true) // not allowed to upload here
    expect(isPermanentRefusal(404)).toBe(true) // the capture is gone
    expect(isPermanentRefusal(409)).toBe(true)
    expect(isPermanentRefusal(413)).toBe(true) // larger than the endpoint accepts
    expect(isPermanentRefusal(422)).toBe(true) // not a file type this API takes
  })

  it('treats an expired token as transient', () => {
    // Signing in again fixes it. Marking the file refused would turn an interrupted session into
    // a walk somebody has to do again.
    expect(isPermanentRefusal(401)).toBe(false)
  })

  it('treats the server asking for time as transient', () => {
    expect(isPermanentRefusal(408)).toBe(false)
    expect(isPermanentRefusal(429)).toBe(false)
    expect(isPermanentRefusal(500)).toBe(false)
    expect(isPermanentRefusal(502)).toBe(false)
    expect(isPermanentRefusal(503)).toBe(false)
  })

  it('treats a failure with no status as transient', () => {
    // A dropped connection reaches the queue with none, and that is the case the serial drain
    // exists for. Unknown has to mean "try again": a file wrongly kept is retried, a file
    // wrongly refused is work somebody has to redo.
    expect(isPermanentRefusal(undefined)).toBe(false)
  })
})

describe('afterUploadFailure', () => {
  /*
    The defect was the `break`, not the classification. Files drain oldest first and the run
    stopped on any failure, so one file the server would never accept was tried first on every
    drain, failed, and stopped the run — every capture queued behind it stayed on the phone,
    untried, for ever. The operator's only way out was to notice and remove it by hand.
  */
  it('carries on past a refusal, so it cannot block the queue behind it', () => {
    expect(afterUploadFailure(403)).toEqual({ status: 'refused', stopDraining: false })
    expect(afterUploadFailure(422)).toEqual({ status: 'refused', stopDraining: false })
  })

  it('stops on a transient failure, because the next file will fail the same way', () => {
    // No signal is the usual case, and draining the rest of the queue into the same wall spends
    // battery on a phone that is probably in a basement.
    expect(afterUploadFailure(undefined)).toEqual({ status: 'failed', stopDraining: true })
    expect(afterUploadFailure(503)).toEqual({ status: 'failed', stopDraining: true })
    expect(afterUploadFailure(401)).toEqual({ status: 'failed', stopDraining: true })
  })

  it('keeps a refused file removable, and never deletes it', () => {
    // The phone holds the only copy of a walk somebody took. A refusal is a reason to stop
    // trying, not a reason to throw the recording away.
    expect(isRemovable(afterUploadFailure(403).status)).toBe(true)
  })
})

describe('statusOf', () => {
  it('reads the status off an API error', () => {
    expect(statusOf({ status: 403 })).toBe(403)
  })

  /*
    The case that would have left the fix half-done. A chunked upload wraps every failure in
    `ResumableUploadInterrupted` so the next drain can continue from the parts already sent, and
    the status is underneath as `cause`. Multipart is the route large files take — and large
    files are exactly the ones refused for being too large or the wrong type — so without this
    walk the status is invisible on the route where it matters most, and a refused video reads as
    "no signal" and blocks everything behind it.
  */
  it('looks through the wrapper a chunked upload throws', () => {
    const wrapped = Object.assign(new Error('The upload was interrupted'), {
      name: 'ResumableUploadInterrupted',
      cause: { status: 422, message: 'That file type is not accepted' },
    })

    expect(statusOf(wrapped)).toBe(422)
    expect(afterUploadFailure(statusOf(wrapped)).status).toBe('refused')
  })

  it('gives up rather than following a cycle', () => {
    const loop: { cause?: unknown } = {}
    loop.cause = loop

    expect(statusOf(loop)).toBeUndefined()
  })

  it('has no status for a dropped connection', () => {
    expect(statusOf(new Error('Network request failed'))).toBeUndefined()
    expect(statusOf(undefined)).toBeUndefined()
    expect(statusOf('nonsense')).toBeUndefined()
  })
})
