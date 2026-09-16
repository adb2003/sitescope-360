/**
 * Which of the three upload routes a file should take.
 *
 * All three end with the same `CaptureFile` row, and they trade against each other:
 *
 * - **Proxied** (`POST /captures/{id}/upload`) streams the body through the API, which checks the
 *   magic bytes and the size cap *before* anything is stored and collapses identical bytes by
 *   checksum. The cost is that the transfer occupies an API worker from first byte to last.
 * - **Direct** (`upload-url` → `PUT` → `files`) sends the bytes to object storage and leaves the
 *   API two small requests. The server re-checks size and type afterwards and deletes whatever
 *   fails, so the guarantees survive — but there is no content checksum, because computing one
 *   would mean reading the object back, which is the cost the route exists to avoid. Uploading
 *   the same file twice therefore produces two rows where the proxied route produces one.
 * - **Resumable** (`multipart` → a `PUT` per part → `complete`) is the direct route cut into
 *   pieces. It costs two extra round trips and buys the only thing that matters on a site
 *   connection: the unit of retry stops being the file. A transfer that drops re-sends one part.
 *
 * So the rule is about size and nothing else, and both boundaries answer the same question —
 * what is the largest amount of work worth losing when the connection goes?
 */

/**
 * Above this, a file goes straight to storage.
 *
 * 32 MiB sits above any panorama this app produces (a 2048×1024 equirectangular JPEG is a few
 * hundred kilobytes; even an 8K one is well under) and far below any walkthrough video, which is
 * the case the direct route was built for. Picking it *between* the two means the common upload
 * keeps content de-duplication and only the transfers that would actually tie up a worker for
 * minutes give it up.
 */
export const DIRECT_UPLOAD_MIN_BYTES = 32 * 1024 * 1024

/**
 * Above this, a file is uploaded in parts.
 *
 * Chosen as the most transfer worth throwing away. A site uplink of a couple of megabits — which
 * is an ordinary day on a half-built floor — moves 64 MiB in roughly four minutes, and four
 * minutes is about as long as a failed attempt can be before re-sending it from the start stops
 * being an inconvenience and starts being the reason the walkthrough never arrives. Below it the
 * two extra round trips a chunked upload costs buy nothing.
 */
export const RESUMABLE_UPLOAD_MIN_BYTES = 64 * 1024 * 1024

export type UploadRoute = 'proxied' | 'direct' | 'resumable'

export function routeFor(
  sizeBytes: number,
  threshold = DIRECT_UPLOAD_MIN_BYTES,
  resumableThreshold = RESUMABLE_UPLOAD_MIN_BYTES,
): UploadRoute {
  // Strictly greater at both boundaries: a file exactly at a threshold keeps the simpler, more
  // strongly guaranteed route. A boundary has to fall on one side deliberately, or it lands
  // wherever the comparison happened to be written.
  if (sizeBytes > resumableThreshold) return 'resumable'
  return sizeBytes > threshold ? 'direct' : 'proxied'
}

/**
 * Split a selection by how it has to be *sent*, preserving the order the operator chose.
 *
 * Two groups rather than three, because the difference that matters here is not which route a
 * file takes but whether it can share a request. Small files go up together in one multipart
 * form; everything else goes one at a time, each picking direct or resumable for itself. Two
 * large uploads racing over a site connection finish no sooner together than in sequence, and in
 * sequence the operator can be told which file is moving.
 *
 * Order is kept because the upload is reported back to them file by file; re-ordering would make
 * the progress line disagree with the list on screen for no reason.
 */
export function planUploads<T extends { size: number }>(
  files: readonly T[],
  threshold = DIRECT_UPLOAD_MIN_BYTES,
): { proxied: T[]; streamed: T[] } {
  const proxied: T[] = []
  const streamed: T[] = []
  for (const file of files) {
    if (routeFor(file.size, threshold) === 'proxied') proxied.push(file)
    else streamed.push(file)
  }
  return { proxied, streamed }
}
