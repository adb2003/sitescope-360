/**
 * Shared styling tokens, mirroring the web app's dark palette so the two feel like one product.
 *
 * Mirroring includes the contrast floor. `inkSubtle` was `#6e7e86`, copied from the web before
 * that value was measured at 3.88:1 against the lightest surface it sits on — below the 4.5:1
 * WCAG AA floor, and it carries every hint, field label and input placeholder in this app. On a
 * phone held in gloves in bright sun, which is the whole point of this client, it was the worst
 * place in the product to have it. `theme.test.ts` holds both halves of that promise: the
 * contrast, and that the palette still matches the web's.
 */

export const theme = {
  canvas: '#0b1116',
  surface: '#12191f',
  surfaceRaised: '#182128',
  line: '#243038',
  lineStrong: '#364650',
  ink: '#e8eef0',
  inkMuted: '#98a8af',
  inkSubtle: '#7c8b92',
  brand: '#2eaa8e',
  brandInk: '#081412',
  good: '#3aba82',
  warn: '#e0a836',
  bad: '#ee6860',
  info: '#60a8e8',
  estimated: '#b094e8',
} as const

export const spacing = {
  xs: 4,
  sm: 8,
  md: 12,
  lg: 16,
  xl: 24,
} as const

/**
 * Minimum touch target. 48dp is the Material guideline, and it is not negotiable here: this app
 * is used one-handed, in gloves, in bright sun.
 */
export const TOUCH_TARGET = 48
