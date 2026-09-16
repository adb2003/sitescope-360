/**
 * Which route a file takes off the phone, and where each chunk of it starts.
 *
 * Pure arithmetic in its own module so it runs under plain Node. Everything here is wrong in the
 * same quiet way if it is wrong at all: a misplaced offset produces an upload that completes, is
 * exactly the right size, and contains the wrong bytes — which no status code and no screen
 * would ever reveal.
 *
 * The thresholds mirror `frontend/src/lib/uploadStrategy.ts`. They are client policy rather than
 * protocol — the server accepts any of the three routes for any size — so the two apps are
 * allowed to differ, but they should not differ by accident. The part *size* is deliberately not
 * duplicated: the server states it in the upload ticket and this file only ever reads it back.
 */

/** Below this a file is streamed through the API, which validates it before storing anything. */
export const DIRECT_UPLOAD_MIN_BYTES = 32 * 1024 * 1024

/**
 * Above this a file goes up in parts.
 *
 * The number is the most transfer worth throwing away. A site uplink of a couple of megabits
 * moves 64 MiB in roughly four minutes, and four minutes is about as long as a failed attempt
 * can be before re-sending it from the start stops being an inconvenience and becomes the reason
 * the walkthrough never arrives — which on a phone is the difference between a capture that
 * exists in the office and one that sits on the device until somebody deletes it.
 */
export const RESUMABLE_UPLOAD_MIN_BYTES = 64 * 1024 * 1024

export type UploadRoute = 'proxied' | 'direct' | 'resumable'

export function routeFor(sizeBytes: number): UploadRoute {
  // Strictly greater at both boundaries: a file exactly at a threshold keeps the simpler, more
  // strongly guaranteed route. The boundary has to fall on one side deliberately, or it lands
  // wherever the comparison happened to be written.
  if (sizeBytes > RESUMABLE_UPLOAD_MIN_BYTES) return 'resumable'
  return sizeBytes > DIRECT_UPLOAD_MIN_BYTES ? 'direct' : 'proxied'
}

/**
 * The byte offset part `partNumber` begins at.
 *
 * Part numbers are 1-based because S3's are, and reading them as 0-based would send part 1's
 * bytes as part 2 — an upload that assembles without complaint into a file shifted by one part.
 */
export function partOffset(partNumber: number, partSize: number): number {
  if (!Number.isInteger(partNumber) || partNumber < 1) {
    throw new Error(`Part number must be a positive integer, got ${partNumber}`)
  }
  return (partNumber - 1) * partSize
}

/**
 * How many bytes are already in storage, given a ticket.
 *
 * Only used to start the progress report somewhere honest. A resume that counted from zero would
 * show the bar jumping backwards for an upload that is in fact nearly finished, and an operator
 * reading that concludes the transfer was thrown away — the exact fear this route answers.
 *
 * Approximate on purpose: the parts already held are whole ones except possibly the last, so
 * multiplying is right until the final part, and clamping covers that.
 */
export function bytesAlreadySent(
  partCount: number,
  partsRemaining: number,
  partSize: number,
  totalBytes: number,
): number {
  return Math.min(Math.max(partCount - partsRemaining, 0) * partSize, totalBytes)
}
