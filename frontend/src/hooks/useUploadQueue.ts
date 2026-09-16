/**
 * Offline-first upload queue (§32).
 *
 * Construction sites have unreliable connectivity, and a capture that only exists on a phone
 * until the operator finds signal must not be lost. Files are held in **IndexedDB**, not in
 * memory and not in localStorage:
 *
 * - In memory, a tab refresh or a browser tab eviction loses the capture.
 * - localStorage only stores strings, so a 2 GiB video would have to be base64-encoded — a 33%
 *   size increase against a 5–10 MB quota. It simply cannot hold a 360 capture.
 *
 * IndexedDB stores `Blob`s natively, has no practical size limit beyond disk, and survives a
 * reload. The queue drains automatically when connectivity returns, and every entry keeps its
 * capture-session id so a file always reaches the visit it belongs to.
 */

import { useCallback, useEffect, useState } from 'react'

import {
  ResumableUploadInterrupted,
  abandonUpload,
  uploadOne,
  type ResumeHandle,
} from '@/lib/uploads'

const DB_NAME = 'sitescope-uploads'
const DB_VERSION = 1
const STORE = 'pending'

export interface QueuedUpload {
  id: string
  sessionId: string
  name: string
  size: number
  type: string
  queuedAt: number
  attempts: number
  blob: Blob
  /**
   * A chunked upload this file already has parts uploaded for.
   *
   * Stored alongside the blob, and for the same reason: the queue exists because a capture that
   * only lives on one device must survive a reload, and half a transfer is worth surviving too.
   * Without this the parts of an interrupted 2 GiB upload would still be in storage — billable,
   * unreachable, and re-sent from the beginning on the next attempt.
   */
  resume?: ResumeHandle
}

export interface UploadQueueState {
  items: QueuedUpload[]
  flushing: boolean
  enqueue: (files: File[]) => void
  flush: () => Promise<void>
  clear: () => void
}

// ---- IndexedDB plumbing -----------------------------------------------------
let dbPromise: Promise<IDBDatabase | null> | null = null

function openDatabase(): Promise<IDBDatabase | null> {
  if (dbPromise) return dbPromise

  dbPromise = new Promise((resolve) => {
    if (typeof indexedDB === 'undefined') {
      resolve(null)
      return
    }
    const request = indexedDB.open(DB_NAME, DB_VERSION)
    request.onupgradeneeded = () => {
      const db = request.result
      if (!db.objectStoreNames.contains(STORE)) {
        const store = db.createObjectStore(STORE, { keyPath: 'id' })
        store.createIndex('sessionId', 'sessionId', { unique: false })
      }
    }
    request.onsuccess = () => resolve(request.result)
    // A private window or a browser with storage disabled: degrade to no queue rather than
    // breaking the capture screen.
    request.onerror = () => resolve(null)
  })

  return dbPromise
}

async function withStore<T>(
  mode: IDBTransactionMode,
  work: (store: IDBObjectStore) => IDBRequest<T>,
): Promise<T | null> {
  const db = await openDatabase()
  if (!db) return null
  return new Promise((resolve) => {
    const transaction = db.transaction(STORE, mode)
    const request = work(transaction.objectStore(STORE))
    request.onsuccess = () => resolve(request.result)
    request.onerror = () => resolve(null)
  })
}

async function readAll(sessionId: string | null): Promise<QueuedUpload[]> {
  const all = (await withStore<QueuedUpload[]>('readonly', (store) => store.getAll())) ?? []
  const sorted = all.sort((a, b) => a.queuedAt - b.queuedAt)
  return sessionId ? sorted.filter((item) => item.sessionId === sessionId) : sorted
}

async function put(item: QueuedUpload): Promise<void> {
  await withStore('readwrite', (store) => store.put(item) as IDBRequest<IDBValidKey>)
}

async function remove(id: string): Promise<void> {
  await withStore('readwrite', (store) => store.delete(id) as unknown as IDBRequest<undefined>)
}

// ---- hook -------------------------------------------------------------------
export function useUploadQueue(sessionId: string | null): UploadQueueState {
  const [items, setItems] = useState<QueuedUpload[]>([])
  const [flushing, setFlushing] = useState(false)

  const refresh = useCallback(async () => {
    setItems(await readAll(sessionId))
  }, [sessionId])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const flush = useCallback(async () => {
    if (flushing) return
    setFlushing(true)
    try {
      const pending = await readAll(null)
      for (const item of pending) {
        // Upload one at a time: a site connection that just came back will not survive six
        // concurrent multi-hundred-megabyte uploads, and a serial queue makes progress visible.
        //
        // Routed by size exactly as the capture page routes it. Retrying through the API instead
        // would put the largest transfers — the ones most likely to be here, because they are the
        // ones most likely to have failed — back on a worker for minutes each.
        const file = new File([item.blob], item.name, { type: item.type })
        try {
          await uploadOne(item.sessionId, file, undefined, item.resume)
          await remove(item.id)
        } catch (error) {
          // Leave it queued and stop: if this one failed, the next will too. Recording the
          // attempt lets the UI show that it is being retried rather than ignored.
          //
          // A chunked upload also leaves behind its handle, so the next drain continues from the
          // last part that landed. Dropping it here is the failure this whole route was built to
          // remove: the retry would succeed, slowly, having re-sent everything.
          const resume = error instanceof ResumableUploadInterrupted ? error.resume : item.resume
          await put({ ...item, attempts: item.attempts + 1, resume })
          break
        }
      }
    } finally {
      setFlushing(false)
      await refresh()
    }
  }, [flushing, refresh])

  const enqueue = useCallback(
    (files: File[]) => {
      if (!sessionId) return
      void (async () => {
        for (const file of files) {
          await put({
            id: `${Date.now()}-${file.name}-${file.size}`,
            sessionId,
            name: file.name,
            size: file.size,
            type: file.type,
            queuedAt: Date.now(),
            attempts: 0,
            blob: file,
          })
        }
        await refresh()
      })()
    },
    [sessionId, refresh],
  )

  const clear = useCallback(() => {
    void (async () => {
      const pending = await readAll(sessionId)
      for (const item of pending) {
        // A chunked upload has parts sitting in storage. They are not objects — nothing lists
        // them and nothing else in the app will ever look for them again — so if the local
        // record goes without telling the server, those bytes are stranded and, on S3, billed
        // for indefinitely. Best effort: the queue entry is discarded either way, because the
        // operator asked for it.
        if (item.resume) {
          await abandonUpload(item.sessionId, item.name, item.size, item.resume).catch(
            () => undefined,
          )
        }
        await remove(item.id)
      }
      await refresh()
    })()
  }, [sessionId, refresh])

  // Drain automatically when connectivity returns.
  useEffect(() => {
    const onOnline = () => void flush()
    window.addEventListener('online', onOnline)
    return () => window.removeEventListener('online', onOnline)
  }, [flush])

  return { items, flushing, enqueue, flush, clear }
}
