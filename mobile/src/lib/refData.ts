/**
 * The project tree, cached on the device (§32).
 *
 * The capture screen used to fetch this live in three requests — projects, then that project's
 * floors, then that floor's zones — which is three chances to be holding no signal. An operator
 * on a half-built floor could not pick the floor they were standing on, so the capture they came
 * to record never started. The upload queue did not help: nothing was queued yet, and nothing
 * could be.
 *
 * So the tree is fetched once from `/sync`, written to the same SQLite database the upload queue
 * uses, and read from there from then on. Connectivity becomes an optimisation — it makes the
 * cache fresher — rather than a precondition for starting work.
 *
 * The reconciliation rules are in `referenceSync.ts` so they can be tested without a device.
 * This file is the part that cannot be: SQL, and the one network call.
 */

import { api } from './api'
import { openQueue } from './queue'
import {
  needsFullResync,
  reconcile,
  type Reconciliation,
  type SyncCollection,
} from './referenceSync'

/** What the capture screen needs about a project. */
export interface CachedProject {
  id: string
  name: string
  code: string
}

/** Shaped as the capture screen's picker wants it, which is why `building_name` is joined in. */
export interface CachedFloor {
  id: string
  name: string
  building_name: string
  project_id: string
}

export interface CachedZone {
  id: string
  name: string
}

interface ProjectRow {
  id: string
  name: string
  code: string
}
interface BuildingRow {
  id: string
  project_id: string
  name: string
}
interface FloorRow {
  id: string
  building_id: string
  name: string
  level: number
}
interface ZoneRow {
  id: string
  floor_id: string
  name: string
}

interface SyncResponse {
  cursor: string
  full: boolean
  projects: SyncCollection<ProjectRow>
  buildings: SyncCollection<BuildingRow>
  floors: SyncCollection<FloorRow>
  zones: SyncCollection<ZoneRow>
}

export interface ReferenceSyncResult {
  /** Rows written across all collections. */
  upserted: number
  /** Rows dropped because the server no longer has them. */
  deleted: number
  /** True when this was a full sync rather than a delta. */
  full: boolean
  /** True when a hole in the delta forced the *next* sync to be a full one. */
  resyncQueued: boolean
}

const CURSOR_KEY = 'reference_cursor'

/**
 * Create the cache tables.
 *
 * Separate from the queue's own schema, on the same connection. The two have different lifetimes
 * and different consequences: losing the queue loses a capture that exists nowhere else, while
 * losing this costs one sync. Keeping them apart means a future reset of the cache cannot take
 * pending uploads with it.
 */
async function openCache() {
  const db = await openQueue()
  await db.execAsync(`
    CREATE TABLE IF NOT EXISTS ref_projects (
      id          TEXT PRIMARY KEY NOT NULL,
      name        TEXT NOT NULL,
      code        TEXT NOT NULL,
      updated_at  TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ref_buildings (
      id          TEXT PRIMARY KEY NOT NULL,
      project_id  TEXT NOT NULL,
      name        TEXT NOT NULL,
      updated_at  TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ref_floors (
      id          TEXT PRIMARY KEY NOT NULL,
      building_id TEXT NOT NULL,
      name        TEXT NOT NULL,
      level       INTEGER NOT NULL DEFAULT 0,
      updated_at  TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ref_zones (
      id          TEXT PRIMARY KEY NOT NULL,
      floor_id    TEXT NOT NULL,
      name        TEXT NOT NULL,
      updated_at  TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ref_meta (
      key         TEXT PRIMARY KEY NOT NULL,
      value       TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_ref_buildings_project ON ref_buildings(project_id);
    CREATE INDEX IF NOT EXISTS idx_ref_floors_building ON ref_floors(building_id);
    CREATE INDEX IF NOT EXISTS idx_ref_zones_floor ON ref_zones(floor_id);
  `)
  return db
}

type Database = Awaited<ReturnType<typeof openCache>>

async function cachedTimestamps(db: Database, table: string): Promise<Map<string, string>> {
  const rows = await db.getAllAsync<{ id: string; updated_at: string }>(
    `SELECT id, updated_at FROM ${table}`,
  )
  return new Map(rows.map((row) => [row.id, row.updated_at]))
}

/**
 * Pull the project tree and make the cache match it.
 *
 * One transaction for the whole response. A sync interrupted half way — the app backgrounded,
 * the process killed — would otherwise leave floors whose buildings had already been deleted,
 * and the screen would show a floor list with no project to attach it to.
 */
export async function syncReferenceData(): Promise<ReferenceSyncResult> {
  const db = await openCache()
  const cursor = await readCursor(db)

  const response = await api.get<SyncResponse>(cursor ? `/sync?since=${cursor}` : '/sync')

  const [projects, buildings, floors, zones] = await Promise.all([
    cachedTimestamps(db, 'ref_projects'),
    cachedTimestamps(db, 'ref_buildings'),
    cachedTimestamps(db, 'ref_floors'),
    cachedTimestamps(db, 'ref_zones'),
  ])

  const plan = {
    projects: reconcile(response.projects, projects),
    buildings: reconcile(response.buildings, buildings),
    floors: reconcile(response.floors, floors),
    zones: reconcile(response.zones, zones),
  }

  let upserted = 0
  let deleted = 0
  await db.withTransactionAsync(async () => {
    upserted += await writeProjects(db, plan.projects)
    upserted += await writeBuildings(db, plan.buildings)
    upserted += await writeFloors(db, plan.floors)
    upserted += await writeZones(db, plan.zones)

    // Children first: a floor deleted before its zones would leave zones pointing at nothing,
    // and the zone picker would offer them for a floor that is gone.
    deleted += await removeRows(db, 'ref_zones', plan.zones.deleteIds)
    deleted += await removeRows(db, 'ref_floors', plan.floors.deleteIds)
    deleted += await removeRows(db, 'ref_buildings', plan.buildings.deleteIds)
    deleted += await removeRows(db, 'ref_projects', plan.projects.deleteIds)

    // A hole in the delta means something is quietly behind, so the next sync asks for
    // everything. Clearing the cursor is how that is remembered across a restart -- holding it
    // in memory would lose the instruction exactly when the app is killed mid-problem.
    const holes = needsFullResync(Object.values(plan) as Reconciliation<unknown>[])
    if (holes) await db.runAsync('DELETE FROM ref_meta WHERE key = ?', [CURSOR_KEY])
    else await writeCursor(db, response.cursor)
  })

  return {
    upserted,
    deleted,
    full: response.full,
    resyncQueued: needsFullResync(Object.values(plan) as Reconciliation<unknown>[]),
  }
}

async function readCursor(db: Database): Promise<string | null> {
  const row = await db.getFirstAsync<{ value: string }>(
    'SELECT value FROM ref_meta WHERE key = ?',
    [CURSOR_KEY],
  )
  return row?.value ?? null
}

async function writeCursor(db: Database, cursor: string): Promise<void> {
  await db.runAsync(
    'INSERT INTO ref_meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?',
    [CURSOR_KEY, cursor, cursor],
  )
}

async function removeRows(db: Database, table: string, ids: readonly string[]): Promise<number> {
  for (const id of ids) {
    await db.runAsync(`DELETE FROM ${table} WHERE id = ?`, [id])
  }
  return ids.length
}

// Every writer takes its timestamp from `updatedAt`, which `reconcile` read off the manifest.
// Taking it from the record instead would store `undefined` for buildings, floors and zones —
// their response schemas carry `created_at` but not `updated_at` — and the cache would then be
// unable to prove any of them current, turning every sync into a full one.
async function writeProjects(db: Database, plan: Reconciliation<ProjectRow>): Promise<number> {
  for (const { row, updatedAt } of plan.upsert) {
    await db.runAsync(
      `INSERT INTO ref_projects (id, name, code, updated_at) VALUES (?, ?, ?, ?)
         ON CONFLICT(id) DO UPDATE SET name = ?, code = ?, updated_at = ?`,
      [row.id, row.name, row.code, updatedAt, row.name, row.code, updatedAt],
    )
  }
  return plan.upsert.length
}

async function writeBuildings(db: Database, plan: Reconciliation<BuildingRow>): Promise<number> {
  for (const { row, updatedAt } of plan.upsert) {
    await db.runAsync(
      `INSERT INTO ref_buildings (id, project_id, name, updated_at) VALUES (?, ?, ?, ?)
         ON CONFLICT(id) DO UPDATE SET project_id = ?, name = ?, updated_at = ?`,
      [row.id, row.project_id, row.name, updatedAt, row.project_id, row.name, updatedAt],
    )
  }
  return plan.upsert.length
}

async function writeFloors(db: Database, plan: Reconciliation<FloorRow>): Promise<number> {
  for (const { row, updatedAt } of plan.upsert) {
    await db.runAsync(
      `INSERT INTO ref_floors (id, building_id, name, level, updated_at) VALUES (?, ?, ?, ?, ?)
         ON CONFLICT(id) DO UPDATE SET building_id = ?, name = ?, level = ?, updated_at = ?`,
      [
        row.id,
        row.building_id,
        row.name,
        row.level,
        updatedAt,
        row.building_id,
        row.name,
        row.level,
        updatedAt,
      ],
    )
  }
  return plan.upsert.length
}

async function writeZones(db: Database, plan: Reconciliation<ZoneRow>): Promise<number> {
  for (const { row, updatedAt } of plan.upsert) {
    await db.runAsync(
      `INSERT INTO ref_zones (id, floor_id, name, updated_at) VALUES (?, ?, ?, ?)
         ON CONFLICT(id) DO UPDATE SET floor_id = ?, name = ?, updated_at = ?`,
      [row.id, row.floor_id, row.name, updatedAt, row.floor_id, row.name, updatedAt],
    )
  }
  return plan.upsert.length
}

// ---- reads, which is all the capture screen ever does -------------------------
export async function cachedProjects(): Promise<CachedProject[]> {
  const db = await openCache()
  return db.getAllAsync<CachedProject>('SELECT id, name, code FROM ref_projects ORDER BY name')
}

/**
 * Floors of one project, with the building name the picker shows.
 *
 * Joined here rather than stored denormalised so that renaming a building updates every floor
 * under it — the sync carries the building's new name and nothing has to know which floors to
 * rewrite.
 */
export async function cachedFloors(projectId: string): Promise<CachedFloor[]> {
  const db = await openCache()
  return db.getAllAsync<CachedFloor>(
    `SELECT f.id        AS id,
            f.name      AS name,
            b.name      AS building_name,
            b.project_id AS project_id
       FROM ref_floors f
       JOIN ref_buildings b ON b.id = f.building_id
      WHERE b.project_id = ?
      ORDER BY b.name, f.level`,
    [projectId],
  )
}

export async function cachedZones(floorId: string): Promise<CachedZone[]> {
  const db = await openCache()
  return db.getAllAsync<CachedZone>(
    'SELECT id, name FROM ref_zones WHERE floor_id = ? ORDER BY name',
    [floorId],
  )
}

/** Whether anything has ever been cached, so the UI can tell "empty" from "never synced". */
export async function hasCachedReferenceData(): Promise<boolean> {
  const db = await openCache()
  const row = await db.getFirstAsync<{ count: number }>(
    'SELECT COUNT(*) AS count FROM ref_projects',
  )
  return (row?.count ?? 0) > 0
}
