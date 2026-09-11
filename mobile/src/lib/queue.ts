/**
 * Offline-first capture queue (§32).
 *
 * The rule this file exists to enforce: **a capture recorded on site is never lost because the
 * network was not there.** On a construction site connectivity is the exception, not the rule, so
 * everything is written to durable local storage first and uploaded later.
 *
 * Two stores, because they have different durability needs:
 *
 * - **SQLite** (`expo-sqlite`) holds the queue *metadata*: session ids, file paths, attempt
 *   counts, status. It survives app restarts and OS eviction of the JS context.
 * - **The device filesystem** holds the bytes. A 360 video is hundreds of megabytes; copying it
 *   into a database row would be both slow and pointless when the file already exists on disk.
 *   The queue stores a URI, and `expo-file-system` copies the picked file into the app's own
 *   document directory so a later OS cleanup of the picker cache cannot pull it out from under
 *   the pending upload.
 *
 * Capture sessions are created *locally first* with a client-generated id, and the backend's
 * create endpoint is idempotent on that id (`client_session_id`). That is what makes the whole
 * flow safe to retry: the operator can start a session, record, and finish, entirely offline, and
 * a later sync produces exactly one session rather than one per retry attempt.
 */

import * as FileSystem from 'expo-file-system'
import { Platform } from 'react-native'
// Type-only: erased at compile time, so the web bundle never references the native module.
// `expo-sqlite` throws from its *import*, not from its first call, so a runtime guard alone is
// not enough -- the module has to be required lazily, after the platform check.
import type * as SQLiteTypes from 'expo-sqlite'

import { api } from './api'
import { isAlreadyProcessing, sessionsReadyForProcessing } from './syncPolicy'

const DB_NAME = 'sitescope.db'
const UPLOAD_DIR = `${FileSystem.documentDirectory}captures/`

export type QueueStatus = 'pending' | 'uploading' | 'uploaded' | 'failed'

export interface PendingSession {
  id: string
  floorId: string
  zoneId: string | null
  name: string | null
  startedAt: string
  endedAt: string | null
  cameraLabel: string | null
  frameIntervalSeconds: number | null
  notes: string | null
  latitude: number | null
  longitude: number | null
  synced: number
}

export interface PendingFile {
  id: string
  sessionId: string
  localUri: string
  name: string
  sizeBytes: number
  mimeType: string
  status: QueueStatus
  attempts: number
  lastError: string | null
  queuedAt: string
}

let database: SQLiteTypes.SQLiteDatabase | null = null

/**
 * True when the durable queue is usable on this platform.
 *
 * `expo-sqlite` is native-only without the OPFS headers a browser needs, and `expo-file-system`
 * has no document directory on web. Expo web exists here to inspect the interface on a desktop,
 * so the queue reports itself unavailable rather than crashing the app — and the UI says so
 * instead of showing an empty queue that looks like "nothing pending".
 */
export const QUEUE_IS_AVAILABLE = Platform.OS !== 'web'

export class QueueUnavailableError extends Error {
  constructor() {
    super(
      'The upload queue needs the device app: a browser has no durable local database for ' +
        'captures. Camera connection and capture setup can be reviewed here, but nothing is ' +
        'queued or uploaded.',
    )
    this.name = 'QueueUnavailableError'
  }
}

export async function openQueue(): Promise<SQLiteTypes.SQLiteDatabase> {
  if (!QUEUE_IS_AVAILABLE) {
    throw new QueueUnavailableError()
  }
  if (database) return database
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const SQLite = require('expo-sqlite') as typeof SQLiteTypes
  database = await SQLite.openDatabaseAsync(DB_NAME)

  // WAL so a read during an upload does not block, and the schema in one place so a fresh
  // install and an upgrade take the same path.
  await database.execAsync(`
    PRAGMA journal_mode = WAL;

    CREATE TABLE IF NOT EXISTS sessions (
      id                     TEXT PRIMARY KEY NOT NULL,
      floor_id               TEXT NOT NULL,
      zone_id                TEXT,
      name                   TEXT,
      started_at             TEXT NOT NULL,
      ended_at               TEXT,
      camera_label           TEXT,
      frame_interval_seconds REAL,
      notes                  TEXT,
      latitude               REAL,
      longitude              REAL,
      synced                 INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS files (
      id          TEXT PRIMARY KEY NOT NULL,
      session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
      local_uri   TEXT NOT NULL,
      name        TEXT NOT NULL,
      size_bytes  INTEGER NOT NULL DEFAULT 0,
      mime_type   TEXT NOT NULL,
      status      TEXT NOT NULL DEFAULT 'pending',
      attempts    INTEGER NOT NULL DEFAULT 0,
      last_error  TEXT,
      queued_at   TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_files_status ON files(status, queued_at);
    CREATE INDEX IF NOT EXISTS idx_files_session ON files(session_id);
  `)

  await FileSystem.makeDirectoryAsync(UPLOAD_DIR, { intermediates: true }).catch(() => undefined)
  return database
}

// ---- session id -------------------------------------------------------------
/**
 * Generate the same `CAP-YYYYMMDD-HHMMSS-XXXX` shape the backend uses (§33).
 *
 * Generated on the device so the operator has a real session id before any network call. The
 * backend accepts it as `client_session_id` and treats a repeat as the same session.
 */
export function newSessionId(when: Date = new Date()): string {
  const pad = (value: number, width = 2) => String(value).padStart(width, '0')
  const date = `${when.getUTCFullYear()}${pad(when.getUTCMonth() + 1)}${pad(when.getUTCDate())}`
  const time = `${pad(when.getUTCHours())}${pad(when.getUTCMinutes())}${pad(when.getUTCSeconds())}`
  const alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'
  let suffix = ''
  for (let index = 0; index < 4; index += 1) {
    suffix += alphabet.charAt(Math.floor(Math.random() * alphabet.length))
  }
  return `CAP-${date}-${time}-${suffix}`
}

// ---- sessions ---------------------------------------------------------------
export async function createLocalSession(
  session: Omit<PendingSession, 'synced'>,
): Promise<PendingSession> {
  const db = await openQueue()
  await db.runAsync(
    `INSERT OR REPLACE INTO sessions
       (id, floor_id, zone_id, name, started_at, ended_at, camera_label,
        frame_interval_seconds, notes, latitude, longitude, synced)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)`,
    [
      session.id,
      session.floorId,
      session.zoneId,
      session.name,
      session.startedAt,
      session.endedAt,
      session.cameraLabel,
      session.frameIntervalSeconds,
      session.notes,
      session.latitude,
      session.longitude,
    ],
  )
  return { ...session, synced: 0 }
}

export async function finishLocalSession(sessionId: string): Promise<void> {
  const db = await openQueue()
  await db.runAsync('UPDATE sessions SET ended_at = ? WHERE id = ?', [
    new Date().toISOString(),
    sessionId,
  ])
}

export async function listSessions(): Promise<PendingSession[]> {
  const db = await openQueue()
  const rows = await db.getAllAsync<Record<string, unknown>>(
    'SELECT * FROM sessions ORDER BY started_at DESC',
  )
  return rows.map(rowToSession)
}

// ---- files ------------------------------------------------------------------
/**
 * Copy a picked file into the app's document directory and queue it.
 *
 * The copy is the important part. A URI from the image picker or document picker often points
 * into a cache the OS may clear at any time, so queuing the original URI risks a pending upload
 * whose file has vanished.
 */
export async function enqueueFile(
  sessionId: string,
  file: { uri: string; name: string; size?: number; mimeType?: string },
): Promise<PendingFile> {
  const db = await openQueue()
  const id = `${Date.now()}-${Math.random().toString(36).slice(2, 10)}`
  const extension = file.name.includes('.') ? file.name.slice(file.name.lastIndexOf('.')) : ''
  const destination = `${UPLOAD_DIR}${id}${extension}`

  await FileSystem.copyAsync({ from: file.uri, to: destination })
  const info = await FileSystem.getInfoAsync(destination, { size: true })
  const sizeBytes = info.exists && 'size' in info ? (info.size ?? 0) : (file.size ?? 0)

  const record: PendingFile = {
    id,
    sessionId,
    localUri: destination,
    name: file.name,
    sizeBytes,
    mimeType: file.mimeType ?? guessMimeType(file.name),
    status: 'pending',
    attempts: 0,
    lastError: null,
    queuedAt: new Date().toISOString(),
  }

  await db.runAsync(
    `INSERT INTO files
       (id, session_id, local_uri, name, size_bytes, mime_type, status, attempts, last_error, queued_at)
     VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, NULL, ?)`,
    [
      record.id,
      record.sessionId,
      record.localUri,
      record.name,
      record.sizeBytes,
      record.mimeType,
      record.queuedAt,
    ],
  )
  return record
}

export async function listFiles(sessionId?: string): Promise<PendingFile[]> {
  const db = await openQueue()
  const rows = sessionId
    ? await db.getAllAsync<Record<string, unknown>>(
        'SELECT * FROM files WHERE session_id = ? ORDER BY queued_at',
        [sessionId],
      )
    : await db.getAllAsync<Record<string, unknown>>('SELECT * FROM files ORDER BY queued_at')
  return rows.map(rowToFile)
}

export async function pendingCount(): Promise<number> {
  const db = await openQueue()
  const row = await db.getFirstAsync<{ count: number }>(
    "SELECT COUNT(*) AS count FROM files WHERE status IN ('pending', 'failed')",
  )
  return row?.count ?? 0
}

// ---- sync -------------------------------------------------------------------
export interface SyncResult {
  sessionsCreated: number
  filesUploaded: number
  filesFailed: number
  /** Sessions handed to the processing pipeline, so they appear in the web app. */
  sessionsProcessing: number
  errors: string[]
}

/**
 * Push everything pending to the server, in dependency order.
 *
 * Sessions first: a file cannot be uploaded to a session the backend has never heard of. Files
 * are then uploaded **one at a time** — a site connection that has just come back will not
 * survive six concurrent multi-hundred-megabyte uploads, and serial progress is progress the
 * operator can see.
 *
 * A failure stops the run rather than hammering through the whole queue: if this upload failed
 * for want of signal, so will the next.
 *
 * Finally, any session that is finished and fully uploaded is handed to the processing pipeline,
 * because uploading bytes nobody processes leaves the capture invisible in the web app.
 */
export async function sync(): Promise<SyncResult> {
  const db = await openQueue()
  const result: SyncResult = {
    sessionsCreated: 0,
    filesUploaded: 0,
    filesFailed: 0,
    sessionsProcessing: 0,
    errors: [],
  }

  const unsynced = (await listSessions()).filter((session) => session.synced === 0)
  for (const session of unsynced) {
    try {
      // Idempotent on client_session_id: a retry returns the existing session (§32, §33).
      await api.post('/captures', {
        client_session_id: session.id,
        floor_id: session.floorId,
        zone_id: session.zoneId ?? undefined,
        name: session.name ?? undefined,
        started_at: session.startedAt,
        camera_label: session.cameraLabel ?? undefined,
        frame_interval_seconds: session.frameIntervalSeconds ?? undefined,
        notes: session.notes ?? undefined,
        gps_latitude: session.latitude ?? undefined,
        gps_longitude: session.longitude ?? undefined,
      })
      await db.runAsync('UPDATE sessions SET synced = 1 WHERE id = ?', [session.id])
      result.sessionsCreated += 1
    } catch (error) {
      result.errors.push(`Session ${session.id}: ${describe(error)}`)
      // Without the session on the server its files cannot be uploaded; skip them this run.
      continue
    }
  }

  const pending = (await listFiles()).filter(
    (file) => file.status === 'pending' || file.status === 'failed',
  )

  for (const file of pending) {
    const sessionRow = await db.getFirstAsync<{ synced: number }>(
      'SELECT synced FROM sessions WHERE id = ?',
      [file.sessionId],
    )
    if (!sessionRow || sessionRow.synced !== 1) continue

    await db.runAsync("UPDATE files SET status = 'uploading' WHERE id = ?", [file.id])
    try {
      await api.uploadFile(`/captures/${file.sessionId}/upload`, {
        uri: file.localUri,
        name: file.name,
        type: file.mimeType,
      })
      await db.runAsync("UPDATE files SET status = 'uploaded', last_error = NULL WHERE id = ?", [
        file.id,
      ])
      // The server has the bytes; reclaim the device storage.
      await FileSystem.deleteAsync(file.localUri, { idempotent: true }).catch(() => undefined)
      result.filesUploaded += 1
    } catch (error) {
      const message = describe(error)
      await db.runAsync(
        "UPDATE files SET status = 'failed', attempts = attempts + 1, last_error = ? WHERE id = ?",
        [message, file.id],
      )
      result.filesFailed += 1
      result.errors.push(`${file.name}: ${message}`)
      break
    }
  }

  await startProcessingForCompleteSessions(result)
  return result
}

/**
 * Hand finished, fully-uploaded sessions to the pipeline.
 *
 * Without this the chain stops halfway: the bytes reach the server and nothing happens to them.
 * No frames are extracted, no quality is scored, no positions are estimated -- so the capture is
 * invisible in the web app, and the operator who walked the floor has no way to know that from
 * the phone. Uploading was never the goal; being able to *see* the walkthrough is.
 *
 * Two conditions, both necessary. **Finished**, because a session still recording will receive
 * more files and processing it now would produce a partial walkthrough that looks complete. And
 * **every file uploaded**, because the pipeline maps a track across frames — running it on half
 * of them yields a shorter path, not a partial one, with no indication anything is missing.
 *
 * Failures here are recorded, never thrown. The upload is the part that cannot be repeated
 * without walking the site again; processing can be retried from either app at any time, so a
 * pipeline that is busy or briefly unreachable must not turn a successful upload into a failed
 * sync.
 */
async function startProcessingForCompleteSessions(result: SyncResult): Promise<void> {
  const sessions = await listSessions()
  const filesBySession = new Map<string, PendingFile[]>()
  for (const session of sessions) {
    filesBySession.set(session.id, await listFiles(session.id))
  }

  for (const sessionId of sessionsReadyForProcessing(sessions, filesBySession)) {
    try {
      await api.post(`/captures/${sessionId}/process`, {})
      result.sessionsProcessing += 1
    } catch (error) {
      // sync runs whenever connectivity returns, so asking twice is normal, not an error.
      if (isAlreadyProcessing(error)) {
        result.sessionsProcessing += 1
        continue
      }
      result.errors.push(`Processing ${sessionId}: ${describe(error)}`)
    }
  }
}

/**
 * Ask the server to process a session on demand.
 *
 * `sync()` already does this automatically for finished, fully-uploaded sessions. This stays for
 * the cases automation should not cover: retrying after a pipeline failure, and processing a
 * session the operator wants looked at now rather than at the next sync.
 */
export async function requestProcessing(sessionId: string): Promise<void> {
  const files = await listFiles(sessionId)
  const outstanding = files.filter((file) => file.status !== 'uploaded')
  if (outstanding.length) {
    throw new Error(
      `${outstanding.length} file(s) are still waiting to upload. Sync first, then process.`,
    )
  }
  await api.post(`/captures/${sessionId}/process`, {})
}

export async function discardUploaded(): Promise<number> {
  const db = await openQueue()
  const uploaded = (await listFiles()).filter((file) => file.status === 'uploaded')
  for (const file of uploaded) {
    await FileSystem.deleteAsync(file.localUri, { idempotent: true }).catch(() => undefined)
  }
  await db.runAsync("DELETE FROM files WHERE status = 'uploaded'")
  return uploaded.length
}

// ---- helpers ----------------------------------------------------------------
function rowToSession(row: Record<string, unknown>): PendingSession {
  return {
    id: String(row.id),
    floorId: String(row.floor_id),
    zoneId: (row.zone_id as string | null) ?? null,
    name: (row.name as string | null) ?? null,
    startedAt: String(row.started_at),
    endedAt: (row.ended_at as string | null) ?? null,
    cameraLabel: (row.camera_label as string | null) ?? null,
    frameIntervalSeconds: (row.frame_interval_seconds as number | null) ?? null,
    notes: (row.notes as string | null) ?? null,
    latitude: (row.latitude as number | null) ?? null,
    longitude: (row.longitude as number | null) ?? null,
    synced: Number(row.synced ?? 0),
  }
}

function rowToFile(row: Record<string, unknown>): PendingFile {
  return {
    id: String(row.id),
    sessionId: String(row.session_id),
    localUri: String(row.local_uri),
    name: String(row.name),
    sizeBytes: Number(row.size_bytes ?? 0),
    mimeType: String(row.mime_type),
    status: String(row.status) as QueueStatus,
    attempts: Number(row.attempts ?? 0),
    lastError: (row.last_error as string | null) ?? null,
    queuedAt: String(row.queued_at),
  }
}

function guessMimeType(name: string): string {
  const lower = name.toLowerCase()
  if (lower.endsWith('.png')) return 'image/png'
  if (lower.endsWith('.mp4')) return 'video/mp4'
  if (lower.endsWith('.mov')) return 'video/quicktime'
  return 'image/jpeg'
}

function describe(error: unknown): string {
  if (error instanceof Error) return error.message
  return String(error)
}
