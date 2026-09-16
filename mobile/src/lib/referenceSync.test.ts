/**
 * Applying a sync response to the device's cache.
 *
 * Every mistake here is silent. Delete too much and the operator opens the app on a floor with
 * no signal to an empty project list — the exact situation the cache exists to prevent. Delete
 * too little and they are offered a floor that no longer exists, and find out when the upload is
 * refused, back in the office, after the walk.
 *
 * So these tests are about what survives rather than what arrives: the `changed` half is a list
 * the server hands over, and copying it is not where this can go wrong.
 */

import { describe, expect, it } from 'vitest'

import { needsFullResync, reconcile, type SyncCollection } from './referenceSync'

interface Row {
  id: string
  name: string
}

const row = (id: string, name = id.toUpperCase()): Row => ({ id, name })
const entry = (id: string, updated_at: string) => ({ id, updated_at })

function collection(changed: Row[], manifest: Array<{ id: string; updated_at: string }>) {
  return { changed, manifest } satisfies SyncCollection<Row>
}

const T1 = '2026-09-14T10:00:00Z'
const T2 = '2026-09-15T10:00:00Z'

describe('reconcile', () => {
  it('writes what arrived, timestamped from the manifest', () => {
    // Not from the record: `BuildingOut`, `FloorOut` and `ZoneOut` carry `created_at` and no
    // `updated_at`, so reading it off the record would cache three of the four collections with
    // `undefined` — after which nothing can be proved current and every sync is a full one.
    const result = reconcile(collection([row('a')], [entry('a', T1)]), new Map())

    expect(result.upsert).toEqual([{ row: row('a'), updatedAt: T1 }])
    expect(result.deleteIds).toEqual([])
    expect(result.staleIds).toEqual([])
  })

  it('refuses to cache a changed row the manifest does not mention', () => {
    // The server contradicting itself: `changed` is meant to be a subset of `manifest`. There is
    // no timestamp to store, and inventing one would make the row look current for ever.
    const result = reconcile(collection([row('orphan')], []), new Map())

    expect(result.upsert).toEqual([])
    expect(result.staleIds).toEqual(['orphan'])
    expect(needsFullResync([result])).toBe(true)
  })

  it('deletes what the manifest no longer lists', () => {
    // The case a change feed cannot express at all: the row is gone, so nothing carries its
    // timestamp, so nothing in `changed` can ever mention it.
    const local = new Map([
      ['a', T1],
      ['gone', T1],
    ])

    const result = reconcile(collection([], [entry('a', T1)]), local)

    expect(result.deleteIds).toEqual(['gone'])
  })

  it('keeps rows the manifest still lists even when nothing changed', () => {
    // The whole cache is at stake here. A quiet sync sends an empty `changed` and a full
    // manifest; treating the manifest as the changed set would delete everything.
    const local = new Map([
      ['a', T1],
      ['b', T1],
    ])

    const result = reconcile(collection([], [entry('a', T1), entry('b', T1)]), local)

    expect(result.deleteIds).toEqual([])
    expect(result.staleIds).toEqual([])
  })

  it('flags a row the manifest says is newer than the cached copy', () => {
    // A hole in the delta: the manifest knows about a change the change feed did not carry.
    // Serving the cached record would be serving something known to be behind.
    const result = reconcile(collection([], [entry('a', T2)]), new Map([['a', T1]]))

    expect(result.staleIds).toEqual(['a'])
    expect(needsFullResync([result])).toBe(true)
  })

  it('does not flag a row that arrived in the same response', () => {
    // Its manifest entry is newer than the cached copy *because* it changed, which is the
    // ordinary case — flagging it would make every real update trigger a full resync.
    const result = reconcile(collection([row('a')], [entry('a', T2)]), new Map([['a', T1]]))

    expect(result.staleIds).toEqual([])
    expect(needsFullResync([result])).toBe(false)
  })

  it('flags a manifest row that is missing locally and did not arrive', () => {
    const result = reconcile(collection([], [entry('new', T1)]), new Map())

    expect(result.staleIds).toEqual(['new'])
  })

  it('compares timestamps as instants, not as strings', () => {
    // ISO-8601 does not order lexicographically across formats: "…00Z" sorts *after*
    // "…00.500000+00:00" because "Z" is a later codepoint than ".". So a string comparison here
    // calls the server's newer record older than the cached one, and never resyncs it — the
    // cache keeps serving a floor name that was changed half a second later, for ever.
    const cached = '2026-09-15T10:00:00Z'
    const server = '2026-09-15T10:00:00.500000+00:00'

    const result = reconcile(collection([], [entry('a', server)]), new Map([['a', cached]]))

    expect(server < cached).toBe(true) // the trap, spelled out
    expect(result.staleIds).toEqual(['a']) // and the behaviour that avoids it
  })

  it('treats equal timestamps as current', () => {
    const result = reconcile(collection([], [entry('a', T1)]), new Map([['a', T1]]))

    expect(result.staleIds).toEqual([])
  })

  it('treats an unreadable timestamp as reason to resync', () => {
    // A cached value written by an older version, or corrupted. It cannot be *proved* current,
    // and the cheap, safe answer is to fetch it again rather than to trust it.
    const result = reconcile(collection([], [entry('a', T1)]), new Map([['a', 'not a date']]))

    expect(result.staleIds).toEqual(['a'])
  })

  it('handles an empty response against an empty cache', () => {
    const result = reconcile(collection([], []), new Map())

    expect(result).toEqual({ upsert: [], deleteIds: [], staleIds: [] })
    expect(needsFullResync([result])).toBe(false)
  })

  it('would wipe the cache if the manifest were ever empty, which is the server contract', () => {
    // Recorded deliberately: the manifest is defined as the complete set, so an empty one means
    // "hold nothing". That is correct here and is exactly why the backend has a test asserting a
    // quiet sync still returns a full manifest — the two halves of one contract.
    const local = new Map([
      ['a', T1],
      ['b', T1],
    ])

    expect(reconcile(collection([], []), local).deleteIds).toEqual(['a', 'b'])
  })
})

describe('needsFullResync', () => {
  it('is true when any collection has a hole', () => {
    const clean = reconcile(collection([], []), new Map())
    const holed = reconcile(collection([], [entry('a', T2)]), new Map([['a', T1]]))

    expect(needsFullResync([clean, clean])).toBe(false)
    expect(needsFullResync([clean, holed])).toBe(true)
  })
})
