/**
 * Expo implementation of {@link UploadIO}.
 *
 * The part that genuinely cannot run without a device, kept as thin as the equivalent split in
 * `cameraTransport.ts`: it wires `expo-file-system` and the API client to the interface and makes
 * no decisions of its own.
 *
 * Two things here are React Native's fault rather than choices:
 *
 * 1. **There is no `Blob.slice` for a file on disk.** A browser hands `XMLHttpRequest` a view of
 *    the file and the platform streams it; here a byte range has to be read out and written to
 *    its own file first. The read goes through base64, which is the only encoding
 *    `readAsStringAsync` offers for binary — so one part (8 MiB by default) exists briefly as an
 *    11 MB string. That is the cost of the route on this platform, and it is bounded by the part
 *    size rather than the file size, which is the point: the alternative was holding nothing at
 *    all and re-sending gigabytes.
 * 2. **`uploadAsync` does the streaming.** `fetch` with a file body is not supported, and
 *    reading the file into JS to POST it would defeat the whole exercise.
 */

import * as FileSystem from 'expo-file-system'

import { ApiError, api } from './api'
import type { LocalFile, UploadIO } from './uploads'

/** Parts are written here, and deleted as soon as they are sent. */
export const PART_DIR = `${FileSystem.documentDirectory}upload-parts/`

let partCounter = 0

async function ensurePartDir(): Promise<void> {
  const info = await FileSystem.getInfoAsync(PART_DIR)
  if (!info.exists) {
    await FileSystem.makeDirectoryAsync(PART_DIR, { intermediates: true })
  }
}

export const expoUploadIO: UploadIO = {
  post: (path, body) => api.post(path, body),

  postFile: (path, file: LocalFile) =>
    api.uploadFile(path, { uri: file.uri, name: file.name, type: file.mimeType }),

  async putFile(url, fileUri, headers) {
    const result = await FileSystem.uploadAsync(url, fileUri, {
      httpMethod: 'PUT',
      uploadType: FileSystem.FileSystemUploadType.BINARY_CONTENT,
      headers,
    })
    if (result.status < 200 || result.status >= 300) {
      // Storage errors are not the API's envelope, so there is no `code` to read. The status is
      // what the caller acts on -- 403 for an expired link, 404 for an upload that is gone.
      throw new ApiError(
        result.status,
        `http_${result.status}`,
        result.status === 403
          ? 'The upload link expired before this part finished'
          : `Storage rejected the upload (HTTP ${result.status})`,
      )
    }
  },

  async writeSlice(uri, position, length) {
    await ensurePartDir()
    // The counter keeps two parts of the same file from colliding if this is ever called
    // concurrently. It is not a security boundary -- the directory is the app's own.
    partCounter += 1
    const destination = `${PART_DIR}part-${Date.now()}-${partCounter}.bin`
    const slice = await FileSystem.readAsStringAsync(uri, {
      encoding: FileSystem.EncodingType.Base64,
      position,
      length,
    })
    await FileSystem.writeAsStringAsync(destination, slice, {
      encoding: FileSystem.EncodingType.Base64,
    })
    return destination
  },

  async remove(uri) {
    await FileSystem.deleteAsync(uri, { idempotent: true })
  },
}

/** A part file still here this long after being written belongs to a run that is over. */
const ORPHAN_AGE_MS = 60 * 60 * 1000

/**
 * Delete part files an earlier run left behind, and say how many.
 *
 * Parts are removed as they are sent, but a process killed mid-upload — which is the event this
 * whole route is built around — leaves one on disk. On a device that queued the capture because
 * it is the only copy, that space matters, and nothing else would ever look in this directory
 * again.
 *
 * Age-gated rather than emptying the directory, so this is safe to call while an upload is in
 * flight. Deleting a part another task is about to PUT would fail that upload for no reason.
 */
export async function clearOrphanedParts(): Promise<number> {
  const info = await FileSystem.getInfoAsync(PART_DIR)
  if (!info.exists) return 0

  const cutoff = Date.now() - ORPHAN_AGE_MS
  const names = await FileSystem.readDirectoryAsync(PART_DIR).catch(() => [] as string[])
  let removed = 0
  for (const name of names) {
    const uri = `${PART_DIR}${name}`
    const part = await FileSystem.getInfoAsync(uri)
    // `modificationTime` is in seconds, unlike everything else in JavaScript.
    if (!part.exists || part.modificationTime * 1000 >= cutoff) continue
    await FileSystem.deleteAsync(uri, { idempotent: true }).catch(() => undefined)
    removed += 1
  }
  return removed
}
