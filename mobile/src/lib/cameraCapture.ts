/**
 * Driving a connected camera through one capture, and getting the result onto the phone.
 *
 * The sequence the operator performs on site, in one place so the screen stays presentational:
 *
 *   startRecording   camera begins recording; the operator walks the path
 *   stopRecording    camera stops and names the file (or finalises it asynchronously)
 *   transfer         the file is pulled to the phone and queued for upload
 *
 * The ordering guarantee that matters: **nothing is deleted from the camera.** The recording
 * stays on the device until the operator removes it themselves. A transfer that half-succeeded,
 * an app that was killed, a phone that filled up — none of those can destroy the only copy of a
 * walkthrough.
 */

import { CameraError, type CameraClient, type CameraFile } from './osc'
import { CAMERA_DOWNLOAD_DIR } from './cameraTransport'
import { enqueueFile, type PendingFile } from './queue'

/** How long to wait for a camera that finalises its file after `stopCapture` returns. */
const FINALISE_TIMEOUT_MS = 30_000
const FINALISE_POLL_MS = 2000

export interface TransferProgress {
  file: string
  fraction: number
  bytesWritten: number
}

export async function startRecording(client: CameraClient): Promise<void> {
  await client.startVideo()
}

/**
 * Stop recording and return the file(s) the camera produced.
 *
 * Two paths, because cameras differ. Most name the file in the `stopCapture` response. Some
 * return nothing and finish writing in the background, so this falls back to polling the file
 * list and taking what is newly present. Treating the empty response as an error would strand a
 * recording that actually succeeded.
 */
export async function stopRecording(
  client: CameraClient,
  knownBefore: readonly string[] = [],
): Promise<CameraFile[]> {
  const named = await client.stopVideo()
  if (named.length > 0) return named

  const deadline = Date.now() + FINALISE_TIMEOUT_MS
  const before = new Set(knownBefore)

  while (Date.now() < deadline) {
    await delay(FINALISE_POLL_MS)
    const listed = await client.listFiles(10).catch(() => [])
    const fresh = listed.filter((file) => file.isVideo && !before.has(file.fileUrl))
    if (fresh.length > 0) return fresh
  }

  throw new CameraError(
    'The camera stopped recording but did not report a file within 30 seconds. Check the camera — if the recording is there, it can be imported from the camera tab.',
    'timeout',
  )
}

/** File URLs currently on the camera, so {@link stopRecording} can spot a new one. */
export async function snapshotFileUrls(client: CameraClient): Promise<string[]> {
  const listed = await client.listFiles(20).catch(() => [])
  return listed.map((file) => file.fileUrl)
}

/**
 * Pull one file off the camera and queue it for upload.
 *
 * Downloaded into the camera directory first, then handed to {@link enqueueFile}, which copies it
 * into the queue's own directory and records the row. The extra copy is deliberate: the queue
 * owns the bytes it is responsible for uploading, and a file it merely points at could be
 * cleaned up underneath it.
 */
export async function transferToQueue(
  client: CameraClient,
  sessionId: string,
  file: CameraFile,
  onProgress?: (progress: TransferProgress) => void,
): Promise<PendingFile> {
  // The camera supplies this name, so it is untrusted input used to build a path. Replacing the
  // separators alone already prevents traversal, but the dot runs are collapsed too: leaving
  // ".." in a path this code constructs is a trap for whoever edits it next, and a filename is
  // not worth defending.
  const safeName =
    file.name
      .replace(/[^A-Za-z0-9._-]/g, '_')
      .replace(/\.{2,}/g, '.')
      .replace(/^\.+/, '') || 'capture'
  const destination = `${CAMERA_DOWNLOAD_DIR}${Date.now()}-${safeName}`

  const uri = await client.download(file, destination, (fraction, bytesWritten) =>
    onProgress?.({ file: file.name, fraction, bytesWritten }),
  )

  return enqueueFile(sessionId, {
    uri,
    name: file.name,
    // Only pass a size the camera actually reported; enqueueFile measures the file otherwise.
    size: file.sizeBytes > 0 ? file.sizeBytes : undefined,
    mimeType: file.isVideo ? 'video/mp4' : 'image/jpeg',
  })
}

function delay(milliseconds: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, milliseconds))
}
