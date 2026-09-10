/** Shared styling tokens, mirroring the web app's dark palette so the two feel like one product. */

export const theme = {
  canvas: '#0b1116',
  surface: '#121b1f',
  surfaceRaised: '#182128',
  line: '#243038',
  lineStrong: '#364650',
  ink: '#e8eef0',
  inkMuted: '#98a8af',
  inkSubtle: '#6e7e86',
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
