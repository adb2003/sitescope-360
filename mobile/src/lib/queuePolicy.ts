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
