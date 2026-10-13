/**
 * Text has to be readable on a tablet, outdoors, by someone wearing safety glasses.
 *
 * Every contrast failure in the application came from one token in each theme and from nothing
 * else: `--ink-subtle` measured 3.37:1 in light and 3.88:1 in dark against the lightest surface
 * it sits on, where WCAG AA asks for 4.5:1 at these sizes. It is not decoration — it carries the
 * sidebar's section labels, every table's column headings, the capture-point codes on a floor
 * plan (69 of them on one screen) and the "0" in an open-issues column.
 *
 * This reads the real stylesheet rather than a copy of the numbers, so the guard fails if
 * somebody nudges a token back towards invisible, and it checks every text colour against every
 * surface it can land on rather than one flattering pair.
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'

// Read from the repository rather than imported: under jsdom `import.meta.url` is an http URL,
// and the point is to assert on the file that ships.
const CSS = readFileSync(resolve(process.cwd(), 'src/styles/index.css'), 'utf8')

/** `--name: 1 2 3` from one block of the stylesheet. */
type Rgb = [number, number, number]

function tokens(block: string): Map<string, Rgb> {
  const out = new Map<string, Rgb>()
  for (const match of block.matchAll(/--([\w-]+):\s*(\d+)\s+(\d+)\s+(\d+)\s*;/g)) {
    const [, name, r, g, b] = match
    out.set(String(name), [Number(r), Number(g), Number(b)])
  }
  return out
}

/** A token, or a failure that names the one that is missing rather than reading `undefined`. */
function token(theme: Map<string, Rgb>, name: string): Rgb {
  const value = theme.get(name)
  if (!value) throw new Error(`--${name} is not defined in index.css`)
  return value
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
  const hi = Math.max(first, second)
  const lo = Math.min(first, second)
  return (hi + 0.05) / (lo + 0.05)
}

/**
 * The body of one selector's block.
 *
 * Matched on the selector *followed by a brace*, not on the bare word: the comment above the
 * palette mentions both `:root` and `.dark` in prose, and slicing on the first occurrence of
 * either picked up the comment — which parses to no tokens at all and makes this file pass by
 * checking nothing.
 */
const LIGHT_BLOCK = /^:root\s*\{([\s\S]*?)^\}/m
const DARK_BLOCK = /^\.dark\s*\{([\s\S]*?)^\}/m

function block(pattern: RegExp, name: string): string {
  const body = CSS.match(pattern)?.[1]
  if (!body) throw new Error(`no ${name} block in index.css`)
  return body
}

const light = tokens(block(LIGHT_BLOCK, ':root'))
const dark = tokens(block(DARK_BLOCK, '.dark'))

// Text colours, and everything they are drawn on. `canvas` is the page, `surface` a card,
// `surface-raised` the lightest panel — a token has to clear the bar on all three, because a
// label does not know which one it landed on.
const INK = ['ink', 'ink-muted', 'ink-subtle'] as const
const SURFACES = ['canvas', 'surface', 'surface-raised'] as const

describe.each([
  ['light', light],
  ['dark', dark],
])('%s palette', (themeName, theme) => {
  it('defines every ink and surface token', () => {
    for (const name of [...INK, ...SURFACES]) {
      expect(theme.get(name), `${themeName} is missing --${name}`).toBeDefined()
    }
  })

  it.each(INK)('--%s clears 4.5:1 on every surface', (ink) => {
    for (const surface of SURFACES) {
      const ratio = contrast(token(theme, ink), token(theme, surface))
      expect(
        Number(ratio.toFixed(2)),
        `--${ink} on --${surface} in ${themeName} is ${ratio.toFixed(2)}:1`,
      ).toBeGreaterThanOrEqual(4.5)
    }
  })
})

describe('status colours', () => {
  /*
    Held to the same 4.5:1 as body text, not to the large-text 3:1 they were first written
    against. That exemption assumed badges — bold, and larger. The evidence says otherwise: the
    open-issue count on the dashboard is `text-warn` at 11px and regular weight, and light
    `--warn` measured 3.59:1 on the page background, so the one token the lower bar would have
    excused is the one that needed raising.
  */
  it.each(['good', 'warn', 'bad', 'info', 'estimated'])('--%s is legible in both themes', (name) => {
    for (const [themeName, theme] of [
      ['light', light],
      ['dark', dark],
    ] as const) {
      for (const surface of SURFACES) {
        const ratio = contrast(token(theme, name), token(theme, surface))
        expect(
          Number(ratio.toFixed(2)),
          `--${name} on --${surface} in ${themeName} is ${ratio.toFixed(2)}:1`,
        ).toBeGreaterThanOrEqual(4.5)
      }
    }
  })
})
