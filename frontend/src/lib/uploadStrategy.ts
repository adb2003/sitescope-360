/**
 * Which of the two upload routes a file should take.
 *
 * Both routes end with the same `CaptureFile` row, and they trade against each other:
 *
 * - **Proxied** (`POST /captures/{id}/upload`) streams the body through the API, which checks the
 *   magic bytes and the size cap *before* anything is stored and collapses identical bytes by
 *   checksum. The cost is that the transfer occupies an API worker from first byte to last.
 * - **Direct** (`upload-url` → `PUT` → `files`) sends the bytes to object storage and leaves the
 *   API two small requests. The server re-checks size and type afterwards and deletes whatever
 *   fails, so the guarantees survive — but there is no content checksum, because computing one
 *   would mean reading the object back, which is the cost the route exists to avoid. Uploading
 *   the same file twice therefore produces two rows where the proxied route produces one.
 *
 * So the rule is about size and nothing else: below the threshold the proxied route's stronger
 * guarantees are worth a few seconds of a worker; above it, they are not worth minutes of one.
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

export type UploadRoute = 'proxied' | 'direct'

export function routeFor(sizeBytes: number, threshold = DIRECT_UPLOAD_MIN_BYTES): UploadRoute {
  // Strictly greater: a file exactly at the threshold keeps the stronger guarantees. The boundary
  // has to fall on one side deliberately, or it lands wherever the comparison happened to be
  // written.
  return sizeBytes > threshold ? 'direct' : 'proxied'
}

/**
 * Split a selection into the two routes, preserving the order the operator chose.
 *
 * Order is kept because the upload is reported back to them file by file; re-ordering would make
 * the progress line disagree with the list on screen for no reason.
 */
export function planUploads<T extends { size: number }>(
  files: readonly T[],
  threshold = DIRECT_UPLOAD_MIN_BYTES,
): { proxied: T[]; direct: T[] } {
  const proxied: T[] = []
  const direct: T[] = []
  for (const file of files) {
    if (routeFor(file.size, threshold) === 'direct') direct.push(file)
    else proxied.push(file)
  }
  return { proxied, direct }
}
