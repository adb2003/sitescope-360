/**
 * The palette has to be readable in the place this client is actually used.
 *
 * A phone, one-handed, in gloves, in bright sun — the conditions the touch-target constant in
 * `theme.ts` already calls out. `inkSubtle` carries every hint, field label, input placeholder
 * and file size in the app, and measured 3.88:1 against the lightest surface it sits on, under
 * the 4.5:1 WCAG AA floor. It was that value because it was copied from the web before the web's
 * was measured, which is the second thing worth a test: the two palettes are supposed to be the
 * same product, so they should not drift apart one fix at a time.
 *
 * Lives under `src/lib` because that is what the vitest config collects: everything above it
 * imports React Native, which will not load under plain Node. The palette is a plain object, so
 * it tests from here perfectly well.
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

import { theme } from '../theme'

type Rgb = [number, number, number]

function rgb(hex: string): Rgb {
  const value = hex.replace('#', '')
  return [0, 2, 4].map((at) => parseInt(value.slice(at, at + 2), 16)) as Rgb
}

function luminance([r, g, b]: Rgb): number {
  const channel = (value: number) => {
    const v = value / 255
    return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

function contrast(a: Rgb, b: Rgb): number {
  const first = luminance(a)
  const second = luminance(b)
  return (Math.max(first, second) + 0.05) / (Math.min(first, second) + 0.05)
}

const SURFACES = ['canvas', 'surface', 'surfaceRaised'] as const
const TEXT = ['ink', 'inkMuted', 'inkSubtle', 'good', 'warn', 'bad', 'info', 'estimated'] as const

describe('palette', () => {
  it.each(TEXT)('%s clears 4.5:1 on every surface', (name) => {
    for (const surface of SURFACES) {
      const ratio = contrast(rgb(theme[name]), rgb(theme[surface]))
      expect(
        Number(ratio.toFixed(2)),
        `theme.${name} on theme.${surface} is ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(4.5)
    }
  })

  it('still mirrors the web app, token for token', () => {
    /*
      The reason this file exists at all: `inkSubtle` was raised on the web and left behind here,
      so "mirroring the web's dark palette" quietly stopped being true. Read from the web's
      stylesheet rather than copied, so the next divergence fails here instead of shipping.
    */
    const css = readFileSync(
      resolve(process.cwd(), '..', 'frontend', 'src', 'styles', 'index.css'),
      'utf8',
    )
    const darkBlock = css.match(/^\.dark\s*\{([\s\S]*?)^\}/m)?.[1]
    expect(darkBlock, 'no .dark block in the web stylesheet').toBeTruthy()

    const web = new Map<string, Rgb>()
    for (const match of (darkBlock ?? '').matchAll(/--([\w-]+):\s*(\d+)\s+(\d+)\s+(\d+)\s*;/g)) {
      const [, name, r, g, b] = match
      web.set(String(name), [Number(r), Number(g), Number(b)])
    }

    // `surfaceRaised` here is `--surface-raised` there; the rest share a name.
    const pairs: Array<[keyof typeof theme, string]> = [
      ['canvas', 'canvas'],
      ['surface', 'surface'],
      ['surfaceRaised', 'surface-raised'],
      ['ink', 'ink'],
      ['inkMuted', 'ink-muted'],
      ['inkSubtle', 'ink-subtle'],
      ['good', 'good'],
      ['warn', 'warn'],
      ['bad', 'bad'],
      ['info', 'info'],
      ['estimated', 'estimated'],
    ]

    for (const [here, there] of pairs) {
      const theirs = web.get(there)
      expect(theirs, `the web stylesheet has no --${there}`).toBeDefined()
      expect(rgb(theme[here]), `theme.${here} has drifted from --${there}`).toEqual(theirs)
    }
  })
})
