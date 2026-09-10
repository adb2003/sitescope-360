/**
 * Capture timeline (§16): activity by date, with the bar chart the brief specifies.
 *
 *     10 Sep  ████████████████████ 92% captured
 *     07 Sep  ██████████████       72% captured
 *
 * The percentage is stated with its denominator visible, because "92% captured" is meaningless
 * without knowing 92% of what. Here it is the share of the floor's known capture points visited
 * on that date — a measurement, not an estimate.
 */

import { CalendarClock, GaugeCircle, Images } from 'lucide-react'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { PageBody, PageHeader } from '@/components/layout/AppShell'
import { FrequencyBadge, Metric, ProvenanceTag } from '@/components/status'
import {
  Badge,
  Button,
  Callout,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Select,
  Skeleton,
} from '@/components/ui'
import {
  useCaptures,
  useFrequency,
  useProjectFloors,
  useTimeline,
} from '@/hooks/queries'
import { useProjectContext } from '@/hooks/useProjectContext'
import { formatDate, formatDateTime, formatNumber, formatPercent } from '@/lib/utils'
import type { TimelineEntry } from '@/types/api'

export function TimelinePage() {
  const { projectId } = useProjectContext()
  const navigate = useNavigate()
  const floors = useProjectFloors(projectId)
  const [floorId, setFloorId] = useState<string | null>(null)

  const timeline = useTimeline(projectId, floorId)
  const frequency = useFrequency(projectId, floorId)
  const captures = useCaptures(projectId ? { project_id: projectId } : {})
  const [selectedDate, setSelectedDate] = useState<string | null>(null)

  // Group by month so a long history reads as a calendar rather than one long list.
  const months = useMemo(() => {
    const grouped = new Map<string, TimelineEntry[]>()
    for (const entry of timeline.data?.entries ?? []) {
      const key = entry.capture_date.slice(0, 7)
      const bucket = grouped.get(key) ?? []
      bucket.push(entry)
      grouped.set(key, bucket)
    }
    return [...grouped.entries()]
  }, [timeline.data])

  const selectedSessions = useMemo(() => {
    if (!selectedDate) return []
    const entry = timeline.data?.entries.find((item) => item.capture_date === selectedDate)
    if (!entry) return []
    return (captures.data?.items ?? []).filter((session) =>
      entry.session_ids.includes(session.id),
    )
  }, [selectedDate, timeline.data, captures.data])

  if (!projectId) {
    return (
      <PageBody>
        <Card>
          <EmptyState
            icon={GaugeCircle}
            title="No project selected"
            description="Choose a project from the sidebar to see its capture timeline."
          />
        </Card>
      </PageBody>
    )
  }

  return (
    <>
      <PageHeader
        title="Timeline"
        subtitle="Capture activity by date"
        actions={
          <Select
            value={floorId ?? ''}
            onChange={(event) => {
              setFloorId(event.target.value || null)
              setSelectedDate(null)
            }}
            className="w-48"
            aria-label="Floor"
          >
            <option value="">All floors</option>
            {floors.data?.map((floor) => (
              <option key={floor.id} value={floor.id}>
                {floor.building_name} · {floor.name}
              </option>
            ))}
          </Select>
        }
      />

      <PageBody className="space-y-4">
        {timeline.isError ? (
          <Card>
            <ErrorState message={timeline.error?.message} onRetry={() => void timeline.refetch()} />
          </Card>
        ) : null}

        <div className="grid gap-3 lg:grid-cols-4">
          <Card className="p-3.5">
            <Metric
              label="Capture dates"
              value={formatNumber(timeline.data?.entries.length)}
              provenance="measured"
            />
          </Card>
          <Card className="p-3.5">
            <Metric
              label="Capture points"
              value={formatNumber(timeline.data?.location_total)}
              provenance="manual"
              hint="Denominator for every percentage below"
            />
          </Card>
          <Card className="p-3.5">
            <Metric
              label="Last capture"
              value={formatDate(frequency.data?.last_capture_at)}
              provenance="measured"
              hint={
                frequency.data?.days_since_last_capture !== null &&
                frequency.data?.days_since_last_capture !== undefined
                  ? `${frequency.data.days_since_last_capture} days ago`
                  : undefined
              }
            />
          </Card>
          <Card className="p-3.5">
            <p className="label">Schedule</p>
            <div className="mt-1.5">
              {frequency.data ? <FrequencyBadge status={frequency.data.status} /> : null}
            </div>
            <p className="mt-1.5 text-2xs text-ink-subtle">
              {frequency.data
                ? `${frequency.data.expected_per_week} per week · next ${
                    frequency.data.next_expected_date
                      ? formatDate(frequency.data.next_expected_date)
                      : '—'
                  }`
                : ''}
            </p>
          </Card>
        </div>

        {timeline.data?.note ? <Callout tone="info">{timeline.data.note}</Callout> : null}

        <Card>
          <CardHeader
            title="Capture history"
            description={
              timeline.data?.location_total
                ? `Percentages are the share of ${timeline.data.location_total} known capture point(s) visited on that date`
                : 'Add capture points to a floor plan to see per-date coverage'
            }
            actions={<ProvenanceTag provenance="measured" />}
          />

          {timeline.isLoading ? (
            <div className="space-y-2 p-4">
              {[0, 1, 2, 3].map((index) => (
                <Skeleton key={index} className="h-6" />
              ))}
            </div>
          ) : !timeline.data?.entries.length ? (
            <EmptyState
              icon={CalendarClock}
              title="No captures yet"
              description="Upload and process a capture to populate the timeline."
              action={<Button onClick={() => navigate('/capture')}>Go to site capture</Button>}
            />
          ) : (
            <div className="divide-y divide-line">
              {months.map(([month, entries]) => (
                <section key={month}>
                  <h3 className="label sticky top-0 bg-surface/95 px-4 py-1.5 backdrop-blur">
                    {new Intl.DateTimeFormat(undefined, {
                      month: 'long',
                      year: 'numeric',
                    }).format(new Date(`${month}-01T00:00:00`))}
                  </h3>
                  <ul>
                    {entries.map((entry) => {
                      const active = entry.capture_date === selectedDate
                      return (
                        <li key={entry.capture_date}>
                          <button
                            type="button"
                            onClick={() =>
                              setSelectedDate(active ? null : entry.capture_date)
                            }
                            className={`flex w-full items-center gap-3 px-4 py-2 text-left transition-colors hover:bg-surface-raised ${
                              active ? 'bg-brand-soft' : ''
                            }`}
                          >
                            <span className="tnum w-16 shrink-0 text-sm font-medium text-ink">
                              {formatDate(entry.capture_date).replace(/\s\d{4}$/, '')}
                            </span>

                            {/* The §16 bar. Rendered with real blocks rather than a div width so
                                it reads the same way the brief drew it. */}
                            <span
                              className="hidden shrink-0 font-mono text-xs tracking-tighter text-brand sm:inline"
                              aria-hidden
                            >
                              {'█'.repeat(Math.max(1, Math.round(entry.coverage_ratio * 20)))}
                              <span className="text-line-strong">
                                {'░'.repeat(20 - Math.max(1, Math.round(entry.coverage_ratio * 20)))}
                              </span>
                            </span>

                            <span className="tnum w-12 shrink-0 text-right text-sm text-ink">
                              {formatPercent(entry.coverage_ratio)}
                            </span>
                            <span className="hidden shrink-0 text-2xs text-ink-subtle sm:inline">
                              captured
                            </span>

                            <span className="ml-auto flex shrink-0 items-center gap-1.5">
                              <Badge tone="neutral">{entry.session_count} session</Badge>
                              <Badge tone="neutral">{entry.frame_count} frames</Badge>
                              {entry.floors.length ? (
                                <span className="hidden max-w-[180px] truncate text-2xs text-ink-muted lg:inline">
                                  {entry.floors.join(', ')}
                                </span>
                              ) : null}
                            </span>
                          </button>
                        </li>
                      )
                    })}
                  </ul>
                </section>
              ))}
            </div>
          )}
        </Card>

        {selectedDate ? (
          <Card>
            <CardHeader
              title={`Captures on ${formatDate(selectedDate)}`}
              actions={
                <Button size="sm" variant="ghost" onClick={() => setSelectedDate(null)}>
                  Clear
                </Button>
              }
            />
            {selectedSessions.length ? (
              <ul className="divide-y divide-line">
                {selectedSessions.map((session) => (
                  <li key={session.id} className="flex flex-wrap items-center gap-2 px-4 py-2">
                    <code className="font-mono text-2xs text-brand">{session.id}</code>
                    <span className="text-xs text-ink-muted">
                      {formatDateTime(session.started_at)}
                    </span>
                    <Badge tone="neutral">{session.frame_count} frames</Badge>
                    <Button
                      size="sm"
                      variant="ghost"
                      icon={Images}
                      className="ml-auto"
                      onClick={() => navigate(`/capture/${session.id}`)}
                    >
                      Open
                    </Button>
                  </li>
                ))}
              </ul>
            ) : (
              <EmptyState title="No session detail loaded for that date" />
            )}
          </Card>
        ) : null}
      </PageBody>
    </>
  )
}
