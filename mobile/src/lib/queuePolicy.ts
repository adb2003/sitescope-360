/**
 * Rules for keeping the upload queue recoverable.
 *
 * Its own module for the same reason as `syncPolicy.ts`: `queue.ts` pulls in expo-sqlite and
 * expo-file-system, so a test of these decisions would end up testing the database instead. The
 * decisions are small and each one is about a capture that would otherwise be stuck on a device
 * with no way off it.
 */

/** The statuses a queued file can be in. Mirrors `QueueStatus` in `queue.ts`. */
export type QueueStatus = 'pending' | 'uploading' | 'uploaded' | 'failed'

export interface QueuedRow {
  id: string
  status: string
}

/**
 * Can this file be removed from the queue by hand?
 *
 * Everything except one already on the server. An uploaded row is cleared by `discardUploaded`,
 * which deletes the local copy but keeps the record of what was sent — removing it here would
 * make "did that file upload?" unanswerable from the phone.
 *
 * Note that `uploading` *is* removable. A row can sit in that state indefinitely if the process
 * died mid-transfer (see {@link stuckUploadIds}), and a status that blocks its own cleanup is how
 * a file becomes permanently unremovable.
 */
export function isRemovable(status: string): boolean {
  return status !== 'uploaded'
}

/**
 * Rows left claiming to be uploading, which at startup can only mean a process that is gone.
 *
 * `sync` marks a row `uploading` before the transfer and moves it to `uploaded` or `failed`
 * afterwards. If the app is killed in between — which on a phone is ordinary, not exceptional —
 * the row keeps that status for ever. It is then invisible to `sync`, which only picks up
 * `pending` and `failed`, so the capture silently never uploads and the operator has no
 * indication why.
 *
 * Nothing can legitimately be uploading at the moment the app opens, so these are all orphans
 * and all safe to return to `pending`.
 */
export function stuckUploadIds(rows: readonly QueuedRow[]): string[] {
  return rows.filter((row) => row.status === 'uploading').map((row) => row.id)
}

/**
 * Will trying this upload again produce the same answer?
 *
 * The queue drains serially and stops on the first failure, which is right when the failure is
 * "no signal" — there is no point burning battery on the rest — and wrong when the server has
 * made a decision. A file the API will never accept is the oldest `failed` row on every drain
 * from then on, so it is tried first, fails, and stops the run: every capture queued behind it
 * stays on the phone, untried, for ever. `isRemovable` lets an operator dig the queue out by
 * hand, which presumes they work out that they have to.
 *
 * Per-project roles made this ordinary rather than exotic. An engineer who is a viewer on *this*
 * job gets 403 on the upload, and no amount of signal changes that. A file type the API does not
 * accept (422) and a capture deleted in the web app (404) have always behaved the same way.
 *
 * Judged on the status code and nothing else:
 *
 *   * **401 is transient.** The token expired; signing in again fixes it, and marking the file
 *     refused would turn an interrupted session into lost work.
 *   * **408, 429 and every 5xx are transient.** Those are the server asking for time.
 *   * **Any other 4xx is permanent.** The request was understood and refused: wrong role, wrong
 *     type, too large, gone. Retrying is how a queue becomes a loop.
 *   * **No status at all is transient.** A network error reaches here with none, and that is the
 *     case the serial drain exists for.
 */
export function isPermanentRefusal(status: number | undefined): boolean {
  if (status === undefined) return false
  if (status === 401 || status === 408 || status === 429) return false
  return status >= 400 && status < 500
}

export interface FailureOutcome {
  /** What to store. `refused` is terminal; `failed` is picked up by the next drain. */
  status: 'failed' | 'refused'
  /** Stop the drain here, or carry on to the next file? */
  stopDraining: boolean
}

/**
 * What to do with a file the upload just failed for.
 *
 * The decision that was wrong was not the classification — it was the `break`. The drain stopped
 * on *any* failure, which is right for the usual one (no signal: there is no point burning
 * battery on the rest of the queue) and wrong once the server has made a decision. Files drain
 * oldest first, so a refused file is tried first on every drain from then on, fails, and stops
 * the run. Every capture queued behind it stays on the phone, untried.
 *
 * So the two halves move together: a refusal is terminal *and* the drain continues past it; a
 * transient failure is retried *and* the drain stops. Returned as a value rather than decided
 * inside `sync`, which needs a database and a device to run at all.
 */
export function afterUploadFailure(status: number | undefined): FailureOutcome {
  return isPermanentRefusal(status)
    ? { status: 'refused', stopDraining: false }
    : { status: 'failed', stopDraining: true }
}

/**
 * The HTTP status behind a failure, looking through whatever wrapped it.
 *
 * `ApiError` carries one, and so does the error `uploadIO` raises for a signed PUT that storage
 * refused. A *chunked* upload carries none: `uploads.ts` wraps every failure on that route in
 * `ResumableUploadInterrupted` so the queue can resume from the parts already sent, and the real
 * error is underneath as `cause`.
 *
 * That wrapper is why this walks the chain. Without it the status is invisible on exactly the
 * route it matters most — the multipart one is for the large files, which are the ones refused
 * for being too large or the wrong type — so a refused video would read as "no signal" and block
 * every capture behind it, which is the whole failure being fixed.
 *
 * Ducks rather than instanceof: this module deliberately imports nothing, so that a test of
 * these decisions is not a test of expo-sqlite.
 */
export function statusOf(error: unknown, depth = 0): number | undefined {
  if (error === null || typeof error !== 'object' || depth > 4) return undefined
  const status = (error as { status?: unknown }).status
  if (typeof status === 'number') return status
  return statusOf((error as { cause?: unknown }).cause, depth + 1)
}
