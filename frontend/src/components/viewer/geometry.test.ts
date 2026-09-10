/**
 * The viewer's spherical convention must match the backend's exactly.
 *
 * This is a genuine cross-language contract, not a formality. `app/vision/panorama.py` maps
 * equirectangular pixels to bearings, and the platform stores annotation positions and frame
 * headings in that frame of reference. If the TypeScript convention disagreed — a flipped sign,
 * a swapped axis — every annotation pin and neighbour hotspot would render in the wrong place,
 * and it would look like a rendering bug rather than a convention mismatch.
 */

import { describe, expect, it } from 'vitest'
import * as THREE from 'three'

import { sphericalToVector, vectorToSpherical } from '@/components/viewer/PanoramaCanvas'

const CLOSE = 1e-9

describe('spherical convention', () => {
  it('places yaw 0 at +Z (the image centre)', () => {
    const forward = sphericalToVector(0, 0)
    expect(forward.x).toBeCloseTo(0, 9)
    expect(forward.y).toBeCloseTo(0, 9)
    expect(forward.z).toBeCloseTo(1, 9)
  })

  it('places yaw +90° at +X (to the right)', () => {
    const right = sphericalToVector(90, 0)
    expect(right.x).toBeCloseTo(1, 9)
    expect(right.z).toBeCloseTo(0, 9)
  })

  it('places yaw -90° at -X (to the left)', () => {
    const left = sphericalToVector(-90, 0)
    expect(left.x).toBeCloseTo(-1, 9)
  })

  it('places yaw 180° at -Z (behind)', () => {
    const behind = sphericalToVector(180, 0)
    expect(behind.z).toBeCloseTo(-1, 9)
  })

  it('places positive pitch upward at +Y', () => {
    const up = sphericalToVector(0, 90)
    expect(up.y).toBeCloseTo(1, 9)
    const down = sphericalToVector(0, -90)
    expect(down.y).toBeCloseTo(-1, 9)
  })

  it('always returns a unit vector', () => {
    for (const yaw of [-180, -90, -37, 0, 12, 90, 179]) {
      for (const pitch of [-85, -45, 0, 30, 85]) {
        expect(sphericalToVector(yaw, pitch).length()).toBeCloseTo(1, 9)
      }
    }
  })

  it('round-trips through vectorToSpherical', () => {
    for (const yaw of [-179, -90, -12, 0, 45, 133]) {
      for (const pitch of [-80, -20, 0, 15, 80]) {
        const back = vectorToSpherical(sphericalToVector(yaw, pitch))
        expect(back.yaw).toBeCloseTo(yaw, 6)
        expect(back.pitch).toBeCloseTo(pitch, 6)
      }
    }
  })

  it('matches the backend formula term by term', () => {
    // Transcribed from app/vision/panorama.py:pixels_to_bearings —
    //   x = cos(lat) * sin(lon), y = sin(lat), z = cos(lat) * cos(lon)
    for (const yawDeg of [-150, -60, 0, 25, 91, 178]) {
      for (const pitchDeg of [-70, -10, 0, 33, 66]) {
        const lon = THREE.MathUtils.degToRad(yawDeg)
        const lat = THREE.MathUtils.degToRad(pitchDeg)
        const expected = new THREE.Vector3(
          Math.cos(lat) * Math.sin(lon),
          Math.sin(lat),
          Math.cos(lat) * Math.cos(lon),
        )

        const actual = sphericalToVector(yawDeg, pitchDeg)
        expect(actual.x).toBeCloseTo(expected.x, 12)
        expect(actual.y).toBeCloseTo(expected.y, 12)
        expect(actual.z).toBeCloseTo(expected.z, 12)
      }
    }
  })

  it('normalises a non-unit input before converting back', () => {
    const scaled = sphericalToVector(42, 17).multiplyScalar(500)
    const back = vectorToSpherical(scaled)
    expect(back.yaw).toBeCloseTo(42, 6)
    expect(back.pitch).toBeCloseTo(17, 6)
  })

  it('clamps out-of-range Y rather than producing NaN', () => {
    // Floating-point drift can push a normalised component just past 1; asin would return NaN.
    const back = vectorToSpherical(new THREE.Vector3(0, 1 + CLOSE, 0))
    expect(Number.isNaN(back.pitch)).toBe(false)
    expect(back.pitch).toBeCloseTo(90, 6)
  })
})
