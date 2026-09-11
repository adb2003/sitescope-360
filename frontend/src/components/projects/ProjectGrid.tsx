/**
 * Project cards — the landing view after sign-in.
 *
 * A table is the right shape for comparing many projects on many columns. It is the wrong shape
 * for *picking one*, which is what somebody signing in almost always wants: a site manager knows
 * which job they are on and needs a target to click, not ten columns to read.
 *
 * So the card shows only what identifies a project and whether it needs attention — name, code,
 * location, how long since the last capture — and the table stays one click away for the cases
 * where the columns genuinely matter.
 *
 * There is no thumbnail. A card wants an image, and the honest options were a floor-plan crop
 * (which needs a plan that many projects do not have) or a panorama frame (which needs a capture,
 * likewise). Rather than ship a placeholder photograph that implies site imagery exists, each
 * card carries a generated mark derived from the project code: distinct enough to recognise at a
 * glance, and obviously not a photograph of anything.
 */

import { ArrowRight, Building2, Camera, Layers, MapPin } from 'lucide-react'

import { Badge, Card } from '@/components/ui'
import { cn, formatRelative } from '@/lib/utils'
import type { ProjectSummary } from '@/types/api'

interface ProjectGridProps {
  projects: ProjectSummary[]
  onOpen: (projectId: string) => void
  /** Days since the last capture past which the card flags it. Mirrors the §20 cadence rule. */
  overdueAfterDays?: number
  className?: string
}

export function ProjectGrid({
  projects,
  onOpen,
  overdueAfterDays = 14,
  className,
}: ProjectGridProps) {
  return (
    <div className={cn('grid gap-3 sm:grid-cols-2 xl:grid-cols-3', className)}>
      {projects.map((project) => (
        <ProjectCard
          key={project.id}
          project={project}
          onOpen={onOpen}
          overdueAfterDays={overdueAfterDays}
        />
      ))}
    </div>
  )
}

function ProjectCard({
  project,
  onOpen,
  overdueAfterDays,
}: {
  project: ProjectSummary
  onOpen: (projectId: string) => void
  overdueAfterDays: number
}) {
  const daysSinceCapture = project.last_capture_at
    ? (Date.now() - new Date(project.last_capture_at).getTime()) / 86_400_000
    : null
  const neverCaptured = project.capture_session_count === 0
  const overdue = daysSinceCapture !== null && daysSinceCapture > overdueAfterDays

  return (
    <Card
      className={cn(
        'group flex cursor-pointer flex-col overflow-hidden p-0 transition',
        'hover:border-brand/60 focus-within:border-brand/60',
      )}
    >
      <button
        type="button"
        onClick={() => onOpen(project.id)}
        className="flex flex-1 flex-col text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-brand"
        aria-label={`Open ${project.name}`}
      >
        <ProjectMark code={project.code} />

        <div className="flex flex-1 flex-col gap-2 p-3">
          <div className="flex items-start gap-2">
            <div className="min-w-0 flex-1">
              <h3 className="truncate font-medium text-ink" title={project.name}>
                {project.name}
              </h3>
              <p className="truncate text-xs text-ink-subtle">{project.code}</p>
            </div>
            <ArrowRight
              className="mt-0.5 size-4 shrink-0 text-ink-subtle transition group-hover:translate-x-0.5 group-hover:text-brand"
              aria-hidden
            />
          </div>

          {project.location ? (
            <p className="flex items-center gap-1 truncate text-xs text-ink-muted">
              <MapPin className="size-3 shrink-0" aria-hidden />
              <span className="truncate">{project.location}</span>
            </p>
          ) : null}

          <div className="mt-auto flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-ink-muted">
            <Stat icon={Building2} value={project.building_count} label="buildings" />
            <Stat icon={Layers} value={project.floor_count} label="floors" />
            <Stat icon={Camera} value={project.capture_session_count} label="captures" />
          </div>

          <div className="flex items-center gap-2">
            {neverCaptured ? (
              // Distinct from "overdue": a project with no captures at all has never started,
              // which is a different problem from one that has fallen behind.
              <Badge tone="neutral">Never captured</Badge>
            ) : overdue ? (
              <Badge tone="warn">
                Last capture {formatRelative(project.last_capture_at as string)}
              </Badge>
            ) : (
              <span className="text-xs text-ink-subtle">
                Last capture {formatRelative(project.last_capture_at as string)}
              </span>
            )}
          </div>
        </div>
      </button>
    </Card>
  )
}

function Stat({
  icon: Icon,
  value,
  label,
}: {
  icon: typeof Building2
  value: number
  label: string
}) {
  return (
    <span className="flex items-center gap-1 tabular-nums" title={`${value} ${label}`}>
      <Icon className="size-3 shrink-0" aria-hidden />
      {value}
    </span>
  )
}

/**
 * A deterministic mark for a project, derived from its code.
 *
 * Deliberately abstract. The alternative was a stock construction photograph, which on a card
 * labelled with a real project reads as *that site* — a claim the platform cannot make until
 * somebody captures it.
 */
function ProjectMark({ code }: { code: string }) {
  let hash = 0
  for (let index = 0; index < code.length; index += 1) {
    hash = (hash * 31 + code.charCodeAt(index)) % 360
  }
  const from = `hsl(${hash} 55% 32%)`
  const to = `hsl(${(hash + 48) % 360} 58% 22%)`

  return (
    <div
      className="relative h-24 w-full shrink-0 overflow-hidden"
      style={{ background: `linear-gradient(135deg, ${from}, ${to})` }}
      aria-hidden
    >
      <svg viewBox="0 0 120 48" className="absolute inset-0 size-full opacity-25">
        {/* A skyline, not a photograph: recognisably "a project", obviously not a site. */}
        {[8, 26, 44, 62, 80, 98].map((x, index) => {
          const height = 14 + ((hash >> index) % 5) * 6
          return (
            <rect
              key={x}
              x={x}
              y={48 - height}
              width={13}
              height={height}
              rx={1}
              className="fill-white"
            />
          )
        })}
      </svg>
      <span className="absolute bottom-1.5 right-2 font-mono text-[11px] font-medium tracking-wide text-white/70">
        {code}
      </span>
    </div>
  )
}
