/**
 * Applying a `/sync` response to a local cache, as arithmetic.
 *
 * Pure and dependency-free so it runs under plain Node. Everything here decides what to *keep*,
 * and every mistake available is silent: delete too much and the operator opens the app on site
 * to an empty floor list; delete too little and they are offered a floor that no longer exists,
 * whose captures the server will refuse once they are back in signal. Neither shows up as an
 * error anywhere.
 *
 * The server sends two halves per collection — `changed` (full records) and `manifest` (id and
 * timestamp for *every* row that should exist). The manifest is the only thing that can express
 * a deletion, because a deleted row has no timestamp left to carry; see the endpoint's own notes
 * in `backend/app/services/sync.py`.
 */

/** One row the server says should exist, and when it last changed. */
export interface ManifestEntry {
  id: string
  updated_at: string
}

export interface SyncCollection<T> {
  changed: T[]
  manifest: ManifestEntry[]
}

/**
 * One row to write, with the timestamp to cache it under.
 *
 * The timestamp comes from the **manifest**, never from the record. Not every record schema
 * carries `updated_at` — `ProjectOut` does, `BuildingOut`, `FloorOut` and `ZoneOut` do not — so
 * reading it off the record stores `undefined` for three of the four collections, and every
 * later sync then finds the cached copy unprovable and resyncs everything. The manifest always
 * has it, for every row, which makes it the only honest source.
 */
export interface PendingWrite<T> {
  row: T
  updatedAt: string
}

export interface Reconciliation<T> {
  /** Records to write. Upserts, because the server re-sends an overlap on every sync. */
  upsert: PendingWrite<T>[]
  /** Rows held locally that the server no longer has. */
  deleteIds: string[]
  /**
   * Rows the manifest says are newer than the local copy, but which `changed` did not carry.
   *
   * Should be empty. When it is not, the delta has a hole in it — a clock, a cursor or a
   * transaction boundary went wrong somewhere — and the honest response is a full resync rather
   * than serving a record known to be behind.
   */
  staleIds: string[]
}

/**
 * Timestamps as milliseconds, never as strings.
 *
 * ISO-8601 does not order lexicographically across formats: `"…22Z"` sorts *after*
 * `"…22.5+00:00"` because `Z` is a later codepoint than `.`, so a string comparison would call
 * a newer row older. The server's exact precision and offset spelling are not ours to fix, so
 * they are parsed instead of compared.
 *
 * An unparseable value returns `NaN`, and every comparison below treats that as "cannot prove
 * the local copy is current" — which errs towards resyncing rather than towards serving stale
 * data.
 */
function millis(iso: string | null | undefined): number {
  if (!iso) return Number.NaN
  return new Date(iso).getTime()
}

/**
 * What to change locally, given one collection of the response and what is cached.
 *
 * `local` maps the id of every cached row to the `updated_at` it was cached with.
 */
export function reconcile<T extends { id: string }>(
  collection: SyncCollection<T>,
  local: ReadonlyMap<string, string>,
): Reconciliation<T> {
  const manifest = new Map(collection.manifest.map((entry) => [entry.id, entry.updated_at]))
  const arriving = new Set(collection.changed.map((row) => row.id))

  const deleteIds: string[] = []
  for (const id of local.keys()) {
    if (!manifest.has(id)) deleteIds.push(id)
  }

  const staleIds: string[] = []
  const upsert: PendingWrite<T>[] = []
  for (const row of collection.changed) {
    const updatedAt = manifest.get(row.id)
    if (updatedAt === undefined) {
      // `changed` should always be a subset of `manifest`. If it is not, the server has
      // contradicted itself and there is no timestamp to cache this row under — so it is left
      // out and the next sync is a full one, rather than stored with a timestamp we invented.
      staleIds.push(row.id)
      continue
    }
    upsert.push({ row, updatedAt })
  }

  for (const [id, updatedAt] of manifest) {
    // A row arriving in `changed` is about to be current, whatever its cached state.
    if (arriving.has(id)) continue
    const cached = millis(local.get(id))
    const server = millis(updatedAt)
    // `>=` rather than `>`: equal is fine, and NaN on either side fails every comparison, which
    // lands in `stale` and triggers a resync -- the safe direction.
    if (!(cached >= server)) staleIds.push(id)
  }

  return { upsert, deleteIds, staleIds }
}

/**
 * Whether a whole response left anything unaccounted for.
 *
 * Collected across collections because the answer is one decision: a single hole anywhere means
 * the next sync should ask for everything. Resyncing the project tree costs about ten kilobytes,
 * which is far less than the cost of an operator working from a record that is quietly behind.
 */
export function needsFullResync(reconciliations: readonly Reconciliation<unknown>[]): boolean {
  return reconciliations.some((result) => result.staleIds.length > 0)
}
