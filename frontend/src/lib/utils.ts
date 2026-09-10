import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'

/** Tailwind-aware class merge, so a later `p-2` actually overrides an earlier `p-4`. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs))
}

// ---- formatting -------------------------------------------------------------
export function formatPercent(value: number | null | undefined, digits = 0): string {
  if (value === null || value === undefined || Number.isNaN(value)) return '—'
  return `${(value * 100).toFixed(digits)}%`
}

export function formatNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return value.toLocaleString()
}

export function formatArea(sqft: number | null | undefined): string {
  if (sqft === null || sqft === undefined) return '—'
  return `${Math.round(sqft).toLocaleString()} sq ft`
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—'
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB', 'TB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`
}

export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 1) return `${Math.round(seconds * 1000)} ms`
  if (seconds < 60) return `${seconds.toFixed(1)} s`
  const minutes = Math.floor(seconds / 60)
  const rest = Math.round(seconds % 60)
  if (minutes < 60) return `${minutes}m ${rest}s`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

const DATE_FMT = new Intl.DateTimeFormat(undefined, {
  day: '2-digit',
  month: 'short',
  year: 'numeric',
})
const DATETIME_FMT = new Intl.DateTimeFormat(undefined, {
  day: '2-digit',
  month: 'short',
  year: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
})
const TIME_FMT = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit' })

export function formatDate(value: string | null | undefined): string {
  const date = parseDate(value)
  return date ? DATE_FMT.format(date) : '—'
}

export function formatDateTime(value: string | null | undefined): string {
  const date = parseDate(value)
  return date ? DATETIME_FMT.format(date) : '—'
}

export function formatTime(value: string | null | undefined): string {
  const date = parseDate(value)
  return date ? TIME_FMT.format(date) : '—'
}

/**
 * "3 days ago" / "in 2 days". Returns `—` for a missing value rather than "just now", because
 * "never captured" and "captured a second ago" must not look the same (§46).
 */
export function formatRelative(value: string | null | undefined): string {
  const date = parseDate(value)
  if (!date) return '—'
  const seconds = (date.getTime() - Date.now()) / 1000
  const absolute = Math.abs(seconds)

  const units: Array<[Intl.RelativeTimeFormatUnit, number]> = [
    ['year', 31536000],
    ['month', 2592000],
    ['week', 604800],
    ['day', 86400],
    ['hour', 3600],
    ['minute', 60],
  ]
  const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' })
  for (const [unit, size] of units) {
    if (absolute >= size) return formatter.format(Math.round(seconds / size), unit)
  }
  return formatter.format(Math.round(seconds), 'second')
}

export function parseDate(value: string | null | undefined): Date | null {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date
}

export function daysBetween(a: string | null | undefined, b: string | null | undefined): number | null {
  const first = parseDate(a)
  const second = parseDate(b)
  if (!first || !second) return null
  return Math.round(Math.abs(second.getTime() - first.getTime()) / 86400000)
}

// ---- labels -----------------------------------------------------------------
/** `extracting_frames` → `Extracting frames`. Used for every enum value the API returns. */
export function humanize(value: string | null | undefined): string {
  if (!value) return '—'
  const spaced = value.replace(/_/g, ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

export function initials(name: string | null | undefined): string {
  if (!name) return '··'
  return name
    .trim()
    .split(/\s+/)
    .slice(0, 2)
    .map((part) => part.charAt(0).toUpperCase())
    .join('')
}

/** Clamp for drag interactions in the floor-plan editor and the viewer. */
export function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max)
}

export function downloadBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  // Revoked on the next tick: revoking synchronously can cancel the download in some browsers.
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
