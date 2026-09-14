/**
 * Which route each file takes.
 *
 * The threshold decides whether an upload keeps content de-duplication or gives it up for not
 * occupying an API worker, so the boundary is worth pinning precisely rather than trusting to
 * whichever way the comparison happened to be written.
 */

import { describe, expect, it } from 'vitest'

import { DIRECT_UPLOAD_MIN_BYTES, planUploads, routeFor } from './uploadStrategy'

const MB = 1024 * 1024

describe('routeFor', () => {
  it('keeps a panorama on the proxied route', () => {
    // A 2048x1024 equirectangular JPEG, which is what this app actually produces.
    expect(routeFor(200 * 1024)).toBe('proxied')
    // And an 8K one, so the common case is nowhere near the boundary.
    expect(routeFor(24 * MB)).toBe('proxied')
  })

  it('sends a walkthrough video straight to storage', () => {
    expect(routeFor(400 * MB)).toBe('direct')
    expect(routeFor(4 * 1024 * MB)).toBe('direct')
  })

  it('treats a file exactly at the threshold as proxied', () => {
    // The boundary has to fall on one side on purpose. Below-or-equal keeps the stronger
    // guarantees, so a file that is exactly at the limit does not silently lose de-duplication.
    expect(routeFor(DIRECT_UPLOAD_MIN_BYTES)).toBe('proxied')
    expect(routeFor(DIRECT_UPLOAD_MIN_BYTES + 1)).toBe('direct')
  })

  it('does not send an empty file anywhere unusual', () => {
    expect(routeFor(0)).toBe('proxied')
  })

  it('honours an explicit threshold', () => {
    expect(routeFor(2 * MB, 1 * MB)).toBe('direct')
    expect(routeFor(2 * MB, 8 * MB)).toBe('proxied')
  })
})

describe('planUploads', () => {
  const file = (name: string, size: number) => ({ name, size })

  it('splits a mixed selection', () => {
    const selection = [
      file('PANO_001.jpg', 300 * 1024),
      file('WALK.mp4', 900 * MB),
      file('PANO_002.jpg', 280 * 1024),
    ]

    const { proxied, direct } = planUploads(selection)

    expect(proxied.map((f) => f.name)).toEqual(['PANO_001.jpg', 'PANO_002.jpg'])
    expect(direct.map((f) => f.name)).toEqual(['WALK.mp4'])
  })

  it('preserves the order the operator chose within each route', () => {
    // The upload is reported back file by file; re-ordering would make the progress line
    // disagree with the list on screen.
    const selection = [
      file('C.mp4', 100 * MB),
      file('A.mp4', 900 * MB),
      file('B.mp4', 50 * MB),
    ]

    expect(planUploads(selection).direct.map((f) => f.name)).toEqual(['C.mp4', 'A.mp4', 'B.mp4'])
  })

  it('handles a selection that is entirely one route', () => {
    expect(planUploads([file('a.jpg', 1024)]).direct).toEqual([])
    expect(planUploads([file('a.mp4', 999 * MB)]).proxied).toEqual([])
  })

  it('handles an empty selection', () => {
    expect(planUploads([])).toEqual({ proxied: [], direct: [] })
  })
})
