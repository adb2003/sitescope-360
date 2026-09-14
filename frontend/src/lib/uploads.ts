/**
 * Sending capture files to the server, by whichever of the two routes suits each one.
 *
 * Its own module because there are two callers that must agree: the capture page's upload button,
 * and the offline queue that retries whatever the button could not finish. When the routing lived
 * only in the button, a failed 500 MB video was re-queued and then retried *through the API* —
 * which is precisely the transfer the direct route exists to keep off it, and the failure is
 * invisible because the upload still succeeds, just slowly and while holding a worker.
 *
 * The routing rule itself is in `uploadStrategy.ts`, separated so it can be tested as arithmetic.
 */

import { ApiError, api, putSignedUpload } from '@/lib/api'
import { routeFor } from '@/lib/uploadStrategy'
import type { CaptureFileDetail, DirectUploadTicket } from '@/types/api'

/** Called repeatedly during a transfer. `total` is the file size, not the selection's. */
export type TransferProgress = (transferred: number, total: number) => void

/**
 * Stream files through the API, which validates them before anything is stored.
 *
 * Takes a list because the endpoint does: several small panoramas are one request rather than one
 * each.
 */
export function uploadProxied(sessionId: string, files: File[]): Promise<CaptureFileDetail[]> {
  const form = new FormData()
  for (const file of files) form.append('files', file)
  return api.upload<CaptureFileDetail[]>(`/captures/${sessionId}/upload`, form)
}

/**
 * Send one file straight to object storage, then ask the server to adopt it.
 *
 * Three steps, and the file does not exist as far as the capture is concerned until the third
 * returns: registration is where the server re-checks the stored object's size and type and
 * deletes it if either fails.
 */
export async function uploadDirect(
  sessionId: string,
  file: File,
  onProgress?: TransferProgress,
): Promise<CaptureFileDetail> {
  const ticket = await api.post<DirectUploadTicket>(`/captures/${sessionId}/upload-url`, {
    filename: file.name,
    // An empty string is what a browser reports for a type it does not recognise; sending it
    // would have the server sign a blank content type that the PUT then has to match exactly.
    content_type: file.type || undefined,
  })
  await putSignedUpload(ticket.upload_url, file, ticket.headers, onProgress)
  return api.post<CaptureFileDetail>(`/captures/${sessionId}/files`, {
    storage_key: ticket.storage_key,
    filename: file.name,
  })
}

/** One file, by whichever route its size calls for. */
export async function uploadOne(
  sessionId: string,
  file: File,
  onProgress?: TransferProgress,
): Promise<CaptureFileDetail> {
  if (routeFor(file.size) === 'direct') return uploadDirect(sessionId, file, onProgress)

  const [created] = await uploadProxied(sessionId, [file])
  if (!created) {
    // One file in, one file out. An empty array would otherwise become an `undefined` that the
    // offline queue records as a successful upload and deletes the only copy of.
    throw new ApiError(502, {
      code: 'empty_upload_response',
      message: 'The server accepted the upload but returned no file',
    })
  }
  return created
}
