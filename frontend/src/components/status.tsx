/**
 * Status and provenance display.
 *
 * This module is where §46 lands in the interface. Three rules it enforces:
 *
 * 1. A number's **provenance** is always visible — measured, estimated, AI-predicted or manual.
 *    `<ProvenanceTag>` is small and always present rather than buried in a tooltip.
 * 2. **Low confidence reads as low confidence.** `<MappingConfidenceNote>` renders the backend's
 *    own explanation, so the UI cannot accidentally present a refused mapping as a result.
 * 3. **Absent is not zero.** `<Metric>` shows an em dash for null, never `0%`.
 */

import {
  Activity,
  AlertTriangle,
  Ban,
  CheckCircle2,
  CircleDashed,
  Clock,
  Hand,
  Loader2,
  MapPin,
  Ruler,
  Sparkles,
  XCircle,
  type LucideIcon,
} from 'lucide-react'
import type { ReactNode } from 'react'

import { Badge, Callout, Progress } from '@/components/ui'
import { cn, formatPercent, humanize } from '@/lib/utils'
import type {
  CaptureFrequencyStatus,
  CaptureSessionStatus,
  DataProvenance,
  JobStatus,
  MappingConfidence,
  QualityVerdict,
} from '@/types/api'

type Tone = 'neutral' | 'brand' | 'good' | 'warn' | 'bad' | 'info' | 'estimated'

// ---- capture session status (§34) ------------------------------------------
const SESSION_STATUS: Record<CaptureSessionStatus, { tone: Tone; icon: LucideIcon; label: string }> = {
  draft: { tone: 'neutral', icon: CircleDashed, label: 'Draft' },
  recording: { tone: 'info', icon: Activity, label: 'Recording' },
  uploading: { tone: 'info', icon: Loader2, label: 'Uploading' },
  uploaded: { tone: 'info', icon: CheckCircle2, label: 'Uploaded' },
  processing: { tone: 'info', icon: Loader2, label: 'Processing' },
  extracting_frames: { tone: 'info', icon: Loader2, label: 'Extracting frames' },
  analyzing: { tone: 'info', icon: Loader2, label: 'Analysing' },
  mapping: { tone: 'info', icon: MapPin, label: 'Mapping' },
  ready: { tone: 'good', icon: CheckCircle2, label: 'Ready' },
  failed: { tone: 'bad', icon: XCircle, label: 'Failed' },
  cancelled: { tone: 'neutral', icon: Ban, label: 'Cancelled' },
}

const IN_FLIGHT: ReadonlySet<CaptureSessionStatus> = new Set([
  'uploading',
  'processing',
  'extracting_frames',
  'analyzing',
])

export function SessionStatusBadge({ status }: { status: CaptureSessionStatus }) {
  const config = SESSION_STATUS[status] ?? SESSION_STATUS.draft
  const Icon = config.icon
  return (
    <Badge tone={config.tone}>
      <Icon className={cn(IN_FLIGHT.has(status) && 'animate-spin')} aria-hidden />
      {config.label}
    </Badge>
  )
}

export function isSessionInFlight(status: CaptureSessionStatus): boolean {
  return IN_FLIGHT.has(status) || status === 'mapping'
}

// ---- job status ------------------------------------------------------------
const JOB_STATUS: Record<JobStatus, Tone> = {
  queued: 'neutral',
  running: 'info',
  complete: 'good',
  failed: 'bad',
  skipped: 'warn',
}

export function JobStatusBadge({ status }: { status: JobStatus }) {
  return (
    <Badge tone={JOB_STATUS[status] ?? 'neutral'}>
      {status === 'running' ? <Loader2 className="animate-spin" aria-hidden /> : null}
      {humanize(status)}
    </Badge>
  )
}

// ---- quality verdict (§10) -------------------------------------------------
const QUALITY: Record<QualityVerdict, { tone: Tone; label: string }> = {
  good: { tone: 'good', label: 'Good' },
  warning: { tone: 'warn', label: 'Warning' },
  poor: { tone: 'bad', label: 'Poor' },
  unusable: { tone: 'bad', label: 'Unusable' },
}

export function QualityBadge({ verdict }: { verdict: QualityVerdict | null | undefined }) {
  if (!verdict) {
    // Not "good" — an unscored frame is genuinely unknown.
    return <Badge tone="neutral">Not scored</Badge>
  }
  const config = QUALITY[verdict]
  return <Badge tone={config.tone}>{config.label}</Badge>
}

// ---- capture frequency (§20) -----------------------------------------------
const FREQUENCY: Record<CaptureFrequencyStatus, { tone: Tone; icon: LucideIcon; label: string }> = {
  ok: { tone: 'good', icon: CheckCircle2, label: 'On schedule' },
  due: { tone: 'warn', icon: Clock, label: 'Due' },
  overdue: { tone: 'bad', icon: AlertTriangle, label: 'Overdue' },
  never_captured: { tone: 'neutral', icon: CircleDashed, label: 'Never captured' },
}

export function FrequencyBadge({ status }: { status: CaptureFrequencyStatus }) {
  const config = FREQUENCY[status] ?? FREQUENCY.never_captured
  const Icon = config.icon
  return (
    <Badge tone={config.tone}>
      <Icon aria-hidden />
      {config.label}
    </Badge>
  )
}

// ---- provenance (§46) ------------------------------------------------------
const PROVENANCE: Record<DataProvenance, { tone: Tone; icon: LucideIcon; label: string; help: string }> = {
  measured: {
    tone: 'good',
    icon: Ruler,
    label: 'Measured',
    help: 'Computed directly from the capture data.',
  },
  estimated: {
    tone: 'estimated',
    icon: Sparkles,
    label: 'Estimated',
    help: 'Derived by computer vision. Relative and scale-free — review before relying on it.',
  },
  ai_predicted: {
    tone: 'estimated',
    icon: Sparkles,
    label: 'AI predicted',
    help: 'Produced by a model. Treat as a suggestion, not a measurement.',
  },
  manual: {
    tone: 'info',
    icon: Hand,
    label: 'Manual',
    help: 'Entered or placed by a person.',
  },
}

export function ProvenanceTag({
  provenance,
  confidence,
  className,
}: {
  provenance: DataProvenance | null | undefined
  confidence?: number | null
  className?: string
}) {
  if (!provenance) return null
  const config = PROVENANCE[provenance]
  const Icon = config.icon
  return (
    <span
      title={config.help}
      className={cn(
        'inline-flex items-center gap-1 text-2xs font-medium uppercase tracking-wide',
        {
          measured: 'text-good',
          estimated: 'text-estimated',
          ai_predicted: 'text-estimated',
          manual: 'text-info',
        }[provenance],
        className,
      )}
    >
      <Icon className="size-3" aria-hidden />
      {config.label}
      {confidence !== null && confidence !== undefined ? (
        <span className="tnum text-ink-subtle">{formatPercent(confidence)}</span>
      ) : null}
    </span>
  )
}

// ---- mapping confidence (§14, §46) -----------------------------------------
const MAPPING: Record<MappingConfidence, { tone: Tone; label: string }> = {
  none: { tone: 'neutral', label: 'No automatic mapping' },
  low: { tone: 'warn', label: 'Low confidence' },
  medium: { tone: 'info', label: 'Medium confidence' },
  high: { tone: 'good', label: 'High confidence' },
}

export function MappingConfidenceBadge({
  confidence,
  score,
}: {
  confidence: MappingConfidence
  score?: number | null
}) {
  const config = MAPPING[confidence] ?? MAPPING.none
  return (
    <Badge tone={config.tone}>
      <MapPin aria-hidden />
      {config.label}
      {score !== null && score !== undefined && confidence !== 'none' ? (
        <span className="tnum ml-0.5">{formatPercent(score)}</span>
      ) : null}
    </Badge>
  )
}

/**
 * Renders the backend's own mapping explanation verbatim.
 *
 * Deliberately not paraphrased in the UI: the note already states the confidence, the number of
 * frame pairs that could not be related, and whether manual placement is required. Restating it
 * in friendlier words is exactly how a "low confidence" result turns into something a site team
 * reads as an answer.
 */
export function MappingConfidenceNote({
  confidence,
  note,
}: {
  confidence: MappingConfidence
  note: string | null | undefined
}) {
  if (!note) return null
  const tone = confidence === 'high' || confidence === 'medium' ? 'estimated' : 'warn'
  return (
    <Callout tone={tone} title={MAPPING[confidence]?.label ?? 'Mapping'}>
      {note}
    </Callout>
  )
}

// ---- metric ----------------------------------------------------------------
/**
 * A single figure with its label and provenance.
 *
 * `value === null` renders an em dash. That distinction is the whole point: a floor with no
 * declared zones has *unknown* coverage, and showing `0%` would be a claim we cannot support.
 */
export function Metric({
  label,
  value,
  unit,
  provenance,
  hint,
  tone,
  progress,
  className,
}: {
  label: ReactNode
  value: ReactNode
  unit?: ReactNode
  provenance?: DataProvenance | null
  hint?: ReactNode
  tone?: 'default' | 'good' | 'warn' | 'bad'
  progress?: number | null
  className?: string
}) {
  const empty = value === null || value === undefined || value === '—'
  return (
    <div className={cn('min-w-0', className)}>
      <p className="label">{label}</p>
      <p
        className={cn(
          'stat mt-0.5 flex items-baseline gap-1',
          empty && 'text-ink-subtle',
          tone === 'good' && 'text-good',
          tone === 'warn' && 'text-warn',
          tone === 'bad' && 'text-bad',
        )}
      >
        {empty ? '—' : value}
        {!empty && unit ? <span className="text-sm font-normal text-ink-muted">{unit}</span> : null}
      </p>
      {progress !== null && progress !== undefined ? (
        <Progress
          value={progress}
          className="mt-1.5"
          estimated={provenance === 'estimated' || provenance === 'ai_predicted'}
        />
      ) : null}
      <div className="mt-1 flex items-center gap-2">
        {provenance ? <ProvenanceTag provenance={provenance} /> : null}
        {hint ? <span className="truncate text-2xs text-ink-subtle">{hint}</span> : null}
      </div>
    </div>
  )
}

/** Shown when an analysis cannot run, in place of a fabricated figure (§46). */
export function UnavailableAnalysis({
  title,
  reason,
  required,
}: {
  title: string
  reason: string
  required?: string[]
}) {
  return (
    <Callout tone="info" title={title}>
      <p>{reason}</p>
      {required?.length ? (
        <ul className="mt-1.5 list-inside list-disc space-y-0.5">
          {required.map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      ) : null}
    </Callout>
  )
}
