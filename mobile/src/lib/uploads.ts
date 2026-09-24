/**
 * Sending a capture file from the phone, by whichever route its size calls for (§7, §32).
 *
 * Until now the mobile app had one route: POST the whole file through the API. That is the wrong
 * one for the client that needs it least badly on paper and most badly in practice — a phone on a
 * half-built floor, holding the only copy of a walkthrough, on a connection that drops. A two
 * gigabyte upload that fails at ninety percent was re-sent from zero, over the connection that
 * had just proved it could not hold.
 *
 * So the three routes the web app has are here too, and the same rule picks between them. The
 * difference is the plumbing: a browser can slice a `File` and hand the view to `XMLHttpRequest`,
 * while React Native has no slice. A part is therefore read out of the file by byte range, written
 * to a temporary file, and streamed from there — see {@link UploadIO}.
 *
 * Everything below takes its I/O as an injected dependency, so it runs under plain Node in the
 * tests. The Expo wiring is in `uploadIO.ts` and does nothing but wiring.
 */

import { bytesAlreadySent, partOffset, routeFor } from './uploadPlan'

/** A file on the device, as the queue records it. */
export interface LocalFile {
  uri: string
  name: string
  mimeType: string
  sizeBytes: number
}

/** Enough to pick a chunked upload back up: the object it is for, and which upload it is. */
export interface ResumeHandle {
  storageKey: string
  uploadId: string
}

/** One part of a chunked upload, as the server describes it. */
export interface UploadPart {
  part_number: number
  upload_url: string
  headers: Record<string, string>
  size_bytes: number
}

export interface MultipartTicket {
  storage_key: string
  upload_id: string
  part_size: number
  part_count: number
  total_bytes: number
  received: number[]
  parts: UploadPart[]
}

export interface DirectTicket {
  storage_key: string
  upload_url: string
  headers: Record<string, string>
}

/**
 * The device-specific half, so the routing above can be tested without a phone.
 *
 * `writeSlice` is the one that only exists because of React Native. There is no `Blob.slice` for
 * a file on disk, so a byte range is read out and written to its own file, which can then be
 * streamed to the network without the whole part passing through the JavaScript heap twice.
 */
export interface UploadIO {
  post<T>(path: string, body: unknown): Promise<T>
  /** Multipart form POST through the API — the proxied route. */
  postFile<T>(path: string, file: LocalFile): Promise<T>
  /** PUT a whole file on disk to a signed URL. */
  putFile(url: string, fileUri: string, headers: Record<string, string>): Promise<void>
  /** Copy a byte range of `uri` into a new temporary file and return its uri. */
  writeSlice(uri: string, position: number, length: number): Promise<string>
  remove(uri: string): Promise<void>
}

/** Reports bytes transferred, out of the file's size. */
export type TransferProgress = (transferred: number, total: number) => void

/**
 * A chunked upload that stopped part way, carrying what is needed to continue it.
 *
 * The handle travels with the failure because the error alone is not enough: the parts that did
 * land are in storage under an id only this attempt knows, and losing it makes those bytes
 * unreachable, still billable, and re-sent from the beginning next time.
 */
export class ResumableUploadInterrupted extends Error {
  readonly resume: ResumeHandle

  constructor(resume: ResumeHandle, cause: unknown) {
    super(cause instanceof Error ? cause.message : 'The upload was interrupted')
    this.name = 'ResumableUploadInterrupted'
    this.resume = resume
    // `cause` is assigned rather than passed to super() so this compiles under the older lib
    // targets React Native still ships with.
    ;(this as { cause?: unknown }).cause = cause
  }
}

/** Thrown when the server says the upload being resumed is gone. */
export function isMissingUpload(error: unknown): boolean {
  return (error as { status?: number } | null)?.status === 404
}

async function uploadDirect<T>(io: UploadIO, sessionId: string, file: LocalFile): Promise<T> {
  const ticket = await io.post<DirectTicket>(`/captures/${sessionId}/upload-url`, {
    filename: file.name,
    content_type: file.mimeType || undefined,
  })
  await io.putFile(ticket.upload_url, file.uri, ticket.headers)
  return io.post<T>(`/captures/${sessionId}/files`, {
    storage_key: ticket.storage_key,
    filename: file.name,
  })
}

async function uploadResumable<T>(
  io: UploadIO,
  sessionId: string,
  file: LocalFile,
  onProgress?: TransferProgress,
  resume?: ResumeHandle,
): Promise<T> {
  const body = { filename: file.name, size_bytes: file.sizeBytes }

  let ticket: MultipartTicket
  if (resume) {
    try {
      ticket = await io.post<MultipartTicket>(`/captures/${sessionId}/multipart/resume`, {
        ...body,
        storage_key: resume.storageKey,
        upload_id: resume.uploadId,
      })
    } catch (error) {
      // The upload is gone: aborted, expired, or completed into an object the server rejected
      // and deleted. Retrying the resume fails identically for ever, and the file is still on
      // the device — which is the entire reason the queue holds it — so start again.
      if (!isMissingUpload(error)) throw error
      return uploadResumable<T>(io, sessionId, file, onProgress)
    }
  } else {
    ticket = await io.post<MultipartTicket>(`/captures/${sessionId}/multipart`, {
      ...body,
      content_type: file.mimeType || undefined,
    })
  }

  const handle: ResumeHandle = { storageKey: ticket.storage_key, uploadId: ticket.upload_id }
  let sent = bytesAlreadySent(
    ticket.part_count,
    ticket.parts.length,
    ticket.part_size,
    file.sizeBytes,
  )
  onProgress?.(sent, file.sizeBytes)

  try {
    for (const part of ticket.parts) {
      const slice = await io.writeSlice(
        file.uri,
        partOffset(part.part_number, ticket.part_size),
        part.size_bytes,
      )
      try {
        await io.putFile(part.upload_url, slice, part.headers)
      } finally {
        // Always, including when the PUT failed. A part left on disk is a copy of the capture
        // on a device that queued the capture because it was short of anything else — and the
        // next attempt writes its own.
        await io.remove(slice).catch(() => undefined)
      }
      sent = Math.min(sent + part.size_bytes, file.sizeBytes)
      onProgress?.(sent, file.sizeBytes)
    }

    return await io.post<T>(`/captures/${sessionId}/multipart/complete`, {
      ...body,
      storage_key: handle.storageKey,
      upload_id: handle.uploadId,
    })
  } catch (error) {
    throw new ResumableUploadInterrupted(handle, error)
  }
}

/**
 * Upload one file by the route its size calls for.
 *
 * `resume` is only ever something a previous attempt handed back, and is only meaningful for the
 * chunked route. It is accepted here rather than only on the chunked path so the queue does not
 * have to re-derive which route a file takes — getting that wrong would silently abandon parts
 * that are already uploaded.
 */
export function uploadFile<T>(
  io: UploadIO,
  sessionId: string,
  file: LocalFile,
  onProgress?: TransferProgress,
  resume?: ResumeHandle,
): Promise<T> {
  const route = routeFor(file.sizeBytes)
  if (route === 'resumable') return uploadResumable<T>(io, sessionId, file, onProgress, resume)
  if (route === 'direct') return uploadDirect<T>(io, sessionId, file)
  return io.postFile<T>(`/captures/${sessionId}/upload`, file)
}

/**
 * Tell the server to throw away a chunked upload nobody is going to finish.
 *
 * Called when the operator removes a queued file that already has parts in storage. Those parts
 * are invisible otherwise — they are not objects, nothing lists them, and on S3 they are billed
 * until something says so — which makes giving up something a client has to *do* rather than
 * something it does by going quiet.
 *
 * Takes the name and size because the endpoint validates the key against them, the same checks
 * every other step of the chunked route applies.
 */
export async function abandonUpload(
  io: UploadIO,
  sessionId: string,
  resume: ResumeHandle,
  file: { name: string; sizeBytes: number },
): Promise<void> {
  await io.post(`/captures/${sessionId}/multipart/abort`, {
    storage_key: resume.storageKey,
    upload_id: resume.uploadId,
    filename: file.name,
    size_bytes: file.sizeBytes,
  })
}
