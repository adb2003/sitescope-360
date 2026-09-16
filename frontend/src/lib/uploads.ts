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
import type { CaptureFileDetail, DirectUploadTicket, MultipartTicket } from '@/types/api'

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

/** Enough to pick a chunked upload back up: the object it is for, and which upload it is. */
export interface ResumeHandle {
  storage_key: string
  upload_id: string
}

/**
 * A chunked upload that stopped part way, carrying what is needed to continue it.
 *
 * Thrown instead of the underlying error because the error alone is not enough. The parts that
 * did land are sitting in storage under an id that only this attempt knows, and if that id is
 * lost the bytes are unreachable *and* still billable — the caller has to start over, which is
 * exactly the cost the route exists to avoid. So the handle travels with the failure.
 */
export class ResumableUploadInterrupted extends Error {
  readonly resume: ResumeHandle

  constructor(resume: ResumeHandle, cause: unknown) {
    super(cause instanceof Error ? cause.message : 'The upload was interrupted')
    this.name = 'ResumableUploadInterrupted'
    this.resume = resume
    this.cause = cause
  }
}

/**
 * Send one file in parts, so that a dropped connection costs one part rather than the transfer.
 *
 * The server decides where the cuts fall and hands back a URL per part; this slices the `File`
 * to match. `File.slice` is a view, not a copy — the bytes are read from disk as the request
 * streams — so a two-gigabyte upload never becomes two gigabytes of JavaScript heap.
 *
 * Parts go up one at a time. Browsers cap concurrent connections per origin anyway, and serially
 * the byte counter only ever moves forwards, which is what the operator is watching.
 *
 * Failures are the interesting case. Nothing is retried here; instead the identifiers leave with
 * the error, as a {@link ResumableUploadInterrupted}, so the caller — the offline queue, normally
 * — can come back later, ask which parts survived, and send only the rest. Swallowing them would
 * quietly turn this back into an all-or-nothing upload that merely costs more round trips.
 */
export async function uploadResumable(
  sessionId: string,
  file: File,
  onProgress?: TransferProgress,
  resume?: ResumeHandle,
): Promise<CaptureFileDetail> {
  const body = { filename: file.name, size_bytes: file.size }

  let ticket: MultipartTicket
  if (resume) {
    try {
      ticket = await api.post<MultipartTicket>(`/captures/${sessionId}/multipart/resume`, {
        ...body,
        ...resume,
      })
    } catch (error) {
      // The upload is gone: aborted, expired, or completed into an object the server then
      // rejected and deleted. Retrying the resume would fail identically for ever, so the only
      // way forward is a fresh upload — the file is still here, which is the whole point of the
      // queue holding it.
      if (!(error instanceof ApiError) || error.status !== 404) throw error
      return uploadResumable(sessionId, file, onProgress)
    }
  } else {
    ticket = await api.post<MultipartTicket>(`/captures/${sessionId}/multipart`, {
      ...body,
      content_type: file.type || undefined,
    })
  }

  const handle: ResumeHandle = { storage_key: ticket.storage_key, upload_id: ticket.upload_id }

  // Parts already in storage are progress the operator has earned; counting from zero on a resume
  // would show the bar going backwards for an upload that is in fact nearly done.
  let sent = Math.min((ticket.part_count - ticket.parts.length) * ticket.part_size, file.size)
  onProgress?.(sent, file.size)

  try {
    for (const part of ticket.parts) {
      const start = (part.part_number - 1) * ticket.part_size
      // A view, not a copy: the bytes are read from disk as the request streams, so a
      // two-gigabyte upload never becomes two gigabytes of JavaScript heap.
      const slice = file.slice(start, start + part.size_bytes)
      const before = sent
      await putSignedUpload(part.upload_url, slice, part.headers, (transferred) =>
        onProgress?.(Math.min(before + transferred, file.size), file.size),
      )
      sent = Math.min(before + part.size_bytes, file.size)
      onProgress?.(sent, file.size)
    }

    return await api.post<CaptureFileDetail>(`/captures/${sessionId}/multipart/complete`, {
      ...body,
      ...handle,
    })
  } catch (error) {
    throw new ResumableUploadInterrupted(handle, error)
  }
}

/**
 * Tell the server to throw away a chunked upload nobody is going to finish.
 *
 * Worth a call of its own because the parts are invisible: they are not objects, so they appear
 * in no listing, nothing else in this application looks for them, and on S3 they are billed
 * until something says otherwise. Giving up has to be something a client *does*, not just
 * something it stops doing.
 */
export async function abandonUpload(
  sessionId: string,
  filename: string,
  sizeBytes: number,
  resume: ResumeHandle,
): Promise<void> {
  await api.post(`/captures/${sessionId}/multipart/abort`, {
    ...resume,
    filename,
    size_bytes: sizeBytes,
  })
}

/**
 * One file, by whichever route its size calls for.
 *
 * `resume` is only meaningful for the chunked route, and is only ever something a previous
 * attempt handed back. It is accepted here rather than only on `uploadResumable` so a caller
 * retrying a queued file does not have to re-derive which route that file takes — getting that
 * wrong would silently abandon the parts already uploaded.
 */
export async function uploadOne(
  sessionId: string,
  file: File,
  onProgress?: TransferProgress,
  resume?: ResumeHandle,
): Promise<CaptureFileDetail> {
  const route = routeFor(file.size)
  if (route === 'resumable') return uploadResumable(sessionId, file, onProgress, resume)
  if (route === 'direct') return uploadDirect(sessionId, file, onProgress)

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
