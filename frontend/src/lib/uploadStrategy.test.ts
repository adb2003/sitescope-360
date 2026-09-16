/**
 * Which route each file takes.
 *
 * Two thresholds, each deciding what an upload gives up. The first trades content
 * de-duplication for not occupying an API worker; the second trades two extra round trips for
 * not having to re-send the whole file when the connection drops. Both boundaries are worth
 * pinning precisely rather than trusting to whichever way the comparison happened to be written,
 * because a file that lands on the wrong side of one still uploads — just with the wrong
 * failure behaviour, which nothing visible would reveal.
 */

import { describe, expect, it } from 'vitest'

import {
  DIRECT_UPLOAD_MIN_BYTES,
  RESUMABLE_UPLOAD_MIN_BYTES,
  planUploads,
  routeFor,
} from './uploadStrategy'

const MB = 1024 * 1024

describe('routeFor', () => {
  it('keeps a panorama on the proxied route', () => {
    // A 2048x1024 equirectangular JPEG, which is what this app actually produces.
    expect(routeFor(200 * 1024)).toBe('proxied')
    // And an 8K one, so the common case is nowhere near either boundary.
    expect(routeFor(24 * MB)).toBe('proxied')
  })

  it('sends a mid-sized file straight to storage in one PUT', () => {
    // Big enough that streaming it through the API is waste, small enough that re-sending it
    // after a failure is an inconvenience rather than the reason the capture never arrives.
    expect(routeFor(40 * MB)).toBe('direct')
  })

  it('sends a walkthrough video up in parts', () => {
    expect(routeFor(400 * MB)).toBe('resumable')
    expect(routeFor(4 * 1024 * MB)).toBe('resumable')
  })

  it('treats a file exactly at either threshold as taking the simpler route', () => {
    // Each boundary falls on one side on purpose. Below-or-equal keeps the simpler, more
    // strongly guaranteed route, so a file exactly at a limit does not silently change
    // behaviour.
    expect(routeFor(DIRECT_UPLOAD_MIN_BYTES)).toBe('proxied')
    expect(routeFor(DIRECT_UPLOAD_MIN_BYTES + 1)).toBe('direct')
    expect(routeFor(RESUMABLE_UPLOAD_MIN_BYTES)).toBe('direct')
    expect(routeFor(RESUMABLE_UPLOAD_MIN_BYTES + 1)).toBe('resumable')
  })

  it('keeps the two thresholds in the order the routes assume', () => {
    // If the resumable threshold ever dropped below the direct one, the direct route would
    // become unreachable — and nothing else in the app would notice, because every file would
    // still upload successfully by one route or another.
    expect(RESUMABLE_UPLOAD_MIN_BYTES).toBeGreaterThan(DIRECT_UPLOAD_MIN_BYTES)
  })

  it('does not send an empty file anywhere unusual', () => {
    expect(routeFor(0)).toBe('proxied')
  })

  it('honours explicit thresholds', () => {
    expect(routeFor(2 * MB, 1 * MB, 100 * MB)).toBe('direct')
    expect(routeFor(2 * MB, 8 * MB, 100 * MB)).toBe('proxied')
    expect(routeFor(2 * MB, 512 * 1024, 1 * MB)).toBe('resumable')
  })
})

describe('planUploads', () => {
  const file = (name: string, size: number) => ({ name, size })

  it('separates the files that can share a request from those that cannot', () => {
    const selection = [
      file('PANO_001.jpg', 300 * 1024),
      file('WALK.mp4', 900 * MB),
      file('PANO_002.jpg', 280 * 1024),
    ]

    const { proxied, streamed } = planUploads(selection)

    expect(proxied.map((f) => f.name)).toEqual(['PANO_001.jpg', 'PANO_002.jpg'])
    expect(streamed.map((f) => f.name)).toEqual(['WALK.mp4'])
  })

  it('groups direct and resumable files together, since both go one at a time', () => {
    // The split is about whether a file can share a multipart form, not about which of the two
    // large-file routes it takes — that choice is `uploadOne`'s, per file.
    const selection = [file('MID.mp4', 40 * MB), file('BIG.mp4', 900 * MB)]

    expect(planUploads(selection).streamed.map((f) => f.name)).toEqual(['MID.mp4', 'BIG.mp4'])
    expect(planUploads(selection).proxied).toEqual([])
  })

  it('preserves the order the operator chose within each group', () => {
    // The upload is reported back file by file; re-ordering would make the progress line
    // disagree with the list on screen.
    const selection = [file('C.mp4', 100 * MB), file('A.mp4', 900 * MB), file('B.mp4', 50 * MB)]

    expect(planUploads(selection).streamed.map((f) => f.name)).toEqual([
      'C.mp4',
      'A.mp4',
      'B.mp4',
    ])
  })

  it('handles a selection that is entirely one group', () => {
    expect(planUploads([file('a.jpg', 1024)]).streamed).toEqual([])
    expect(planUploads([file('a.mp4', 999 * MB)]).proxied).toEqual([])
  })

  it('handles an empty selection', () => {
    expect(planUploads([])).toEqual({ proxied: [], streamed: [] })
  })
})
