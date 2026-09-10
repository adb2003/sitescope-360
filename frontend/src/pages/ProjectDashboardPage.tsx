/** Project dashboard — the §21 layout: overview, capture health, floor status, recent captures. */

import {
  Camera,
  ClipboardList,
  FileText,
  Layers,
  MapPin,
  TriangleAlert,
} from 'lucide-react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { PageBody, PageHeader } from '@/components/layout/AppShell'
import {
  FrequencyBadge,
  MappingConfidenceBadge,
  Metric,
  QualityBadge,
  SessionStatusBadge,
} from '@/components/status'
import {
  Badge,
  Button,
  Callout,
  Card,
  CardHeader,
  EmptyState,
  ErrorState,
  Progress,
  Skeleton,
  Table,
  Td,
  Th,
  Tr,
} from '@/components/ui'
import { useDashboard, useStaleAreas } from '@/hooks/queries'
import { useProjectContext } from '@/hooks/useProjectContext'
import { formatArea, formatDateTime, formatNumber, formatPercent, formatRelative } from '@/lib/utils'

export function ProjectDashboardPage() {
  const params = useParams<{ projectId: string }>()
  const { projectId: contextProjectId, setProjectId } = useProjectContext()
  const projectId = params.projectId ?? contextProjectId ?? undefined
  const navigate = useNavigate()

  const { data, isLoading, isError, error, refetch } = useDashboard(projectId)
  const stale = useStaleAreas(projectId)

  // Keep the sidebar's active project in step with a directly-visited URL.
  if (params.projectId && params.projectId !== contextProjectId) {
    setProjectId(params.projectId)
  }

  if (!projectId) {
    return (
      <PageBody>
        <Card>
          <EmptyState
            icon={Layers}
            title="No project selected"
            description="Choose a project from the sidebar to see its dashboard."
            action={<Button onClick={() => navigate('/projects')}>Browse projects</Button>}
          />
        </Card>
      </PageBody>
    )
  }

  if (isError) {
    return (
      <PageBody>
        <Card>
          <ErrorState message={error?.message} onRetry={() => void refetch()} />
        </Card>
      </PageBody>
    )
  }

  return (
    <>
      <PageHeader
        title={data?.project_name ?? 'Project'}
        subtitle={
          data ? (
            <span className="font-mono text-xs">{data.project_code}</span>
          ) : (
            <Skeleton className="h-3 w-20" />
          )
        }
        breadcrumbs={[{ label: 'Projects', to: '/projects' }, { label: data?.project_name ?? '…' }]}
        actions={
          <>
            <Button variant="secondary" icon={MapPin} onClick={() => navigate('/floorplans')}>
              Floor plans
            </Button>
            <Button variant="primary" icon={Camera} onClick={() => navigate('/capture')}>
              New capture
            </Button>
          </>
        }
      />

      <PageBody className="space-y-4">
        {isLoading ? (
          <div className="grid gap-3 lg:grid-cols-4">
            {[0, 1, 2, 3].map((index) => (
              <Card key={index} className="p-4">
                <Skeleton className="h-3 w-24" />
                <Skeleton className="mt-2 h-7 w-16" />
              </Card>
            ))}
          </div>
        ) : data?.is_empty ? (
          <Card>
            <EmptyState
              icon={Camera}
              title="Nothing captured yet"
              description="Upload a 360 image or video against a floor to populate this dashboard. Until then there is nothing to measure — so nothing is shown as zero."
              action={
                <Button variant="primary" icon={Camera} onClick={() => navigate('/capture')}>
                  Start a capture
                </Button>
              }
            />
          </Card>
        ) : null}

        {data && !data.is_empty ? (
          <>
            {/* Project overview */}
            <div className="grid grid-cols-2 gap-3 lg:grid-cols-6">
              <Card className="p-3.5">
                <Metric label="Floors" value={formatNumber(data.floor_count)} provenance="manual" />
              </Card>
              <Card className="p-3.5">
                <Metric label="Zones" value={formatNumber(data.zone_count)} provenance="manual" />
              </Card>
              <Card className="p-3.5">
                <Metric
                  label="Capture points"
                  value={formatNumber(data.location_count)}
                  provenance="manual"
                />
              </Card>
              <Card className="p-3.5">
                <Metric
                  label="Captures"
                  value={formatNumber(data.session_count)}
                  provenance="measured"
                />
              </Card>
              <Card className="p-3.5">
                <Metric
                  label="360 frames"
                  value={formatNumber(data.frame_count)}
                  provenance="measured"
                />
              </Card>
              <Card className="p-3.5">
                <Metric
                  label="Last capture"
                  value={formatRelative(data.last_capture_at)}
                  provenance="measured"
                  hint={formatDateTime(data.last_capture_at)}
                />
              </Card>
            </div>

            <div className="grid gap-3 lg:grid-cols-3">
              {/* Coverage */}
              <Card className="lg:col-span-2">
                <CardHeader
                  title="Coverage"
                  description={data.coverage.method}
                  actions={<Badge tone="info">Declared, not detected</Badge>}
                />
                <div className="grid gap-4 p-4 sm:grid-cols-3">
                  <Metric
                    label="Overall coverage"
                    value={formatPercent(data.coverage.coverage_ratio)}
                    provenance={data.coverage.provenance}
                    progress={data.coverage.coverage_ratio}
                  />
                  <Metric
                    label="Captured area"
                    value={formatArea(data.coverage.covered_area_sqft)}
                    provenance={data.coverage.provenance}
                    hint="Apportioned from the ratio"
                  />
                  <Metric
                    label="Pending area"
                    value={formatArea(data.coverage.pending_area_sqft)}
                    provenance={data.coverage.provenance}
                  />
                </div>
              </Card>

              {/* Capture health */}
              <Card>
                <CardHeader title="Capture health" />
                <dl className="divide-y divide-line">
                  {[
                    ['Total planned captures', data.capture_health.total_sessions, 'text-ink'],
                    ['Completed', data.capture_health.completed, 'text-good'],
                    ['In progress', data.capture_health.in_progress, 'text-info'],
                    ['Pending', data.capture_health.pending, 'text-warn'],
                    ['Failed', data.capture_health.failed, 'text-bad'],
                  ].map(([label, value, tone]) => (
                    <div key={String(label)} className="flex items-center justify-between px-4 py-2">
                      <dt className="text-sm text-ink-muted">{label}</dt>
                      <dd className={`tnum text-lg font-semibold ${tone}`}>{String(value)}</dd>
                    </div>
                  ))}
                </dl>
              </Card>
            </div>

            <div className="grid gap-3 lg:grid-cols-3">
              {/* Floor status */}
              <Card className="lg:col-span-2">
                <CardHeader
                  title="Floor status"
                  description="Visited capture points against the points declared for each floor"
                />
                {data.floors.length ? (
                  <Table>
                    <thead>
                      <tr>
                        <Th>Floor</Th>
                        <Th className="text-right">Points</Th>
                        <Th className="text-right">Visited</Th>
                        <Th className="w-44">Coverage</Th>
                        <Th>Last capture</Th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.floors.map((floor) => (
                        <Tr key={floor.floor_id}>
                          <Td className="font-medium">{floor.floor_name}</Td>
                          <Td className="tnum text-right">{floor.location_count}</Td>
                          <Td className="tnum text-right">{floor.visited_locations}</Td>
                          <Td>
                            <Progress
                              value={floor.coverage_ratio}
                              showLabel
                              tone={
                                floor.coverage_ratio >= 0.8
                                  ? 'good'
                                  : floor.coverage_ratio >= 0.4
                                    ? 'warn'
                                    : 'bad'
                              }
                            />
                          </Td>
                          <Td className="text-xs text-ink-muted">
                            {formatRelative(floor.last_capture_at)}
                          </Td>
                        </Tr>
                      ))}
                    </tbody>
                  </Table>
                ) : (
                  <EmptyState title="No floors yet" description="Add floors under Projects." />
                )}
              </Card>

              {/* Frequency + quality */}
              <div className="space-y-3">
                <Card>
                  <CardHeader title="Capture schedule" />
                  <div className="space-y-3 p-4">
                    <div className="flex items-center justify-between">
                      <span className="text-sm text-ink-muted">Status</span>
                      <FrequencyBadge status={data.frequency.status} />
                    </div>
                    <dl className="space-y-1.5 text-sm">
                      <Row label="Expected" value={`${data.frequency.expected_per_week} / week`} />
                      <Row
                        label="Last capture"
                        value={formatDateTime(data.frequency.last_capture_at)}
                      />
                      <Row
                        label="Days since"
                        value={
                          data.frequency.days_since_last_capture === null
                            ? '—'
                            : String(data.frequency.days_since_last_capture)
                        }
                      />
                      <Row
                        label="Next expected"
                        value={
                          data.frequency.next_expected_date
                            ? formatDateTime(data.frequency.next_expected_date)
                            : '—'
                        }
                      />
                      <Row label="This week" value={String(data.frequency.captures_this_week)} />
                    </dl>
                  </div>
                </Card>

                <Card>
                  <CardHeader title="Frame quality" description="Classical CV measurements" />
                  <div className="space-y-1.5 p-4">
                    {(
                      [
                        ['Good', data.quality.good, 'bg-good'],
                        ['Warning', data.quality.warning, 'bg-warn'],
                        ['Poor', data.quality.poor, 'bg-bad'],
                        ['Unusable', data.quality.unusable, 'bg-bad'],
                        ['Not scored', data.quality.unscored, 'bg-ink-subtle'],
                      ] as const
                    ).map(([label, count, colour]) => {
                      const total = Math.max(data.frame_count, 1)
                      return (
                        <div key={label} className="flex items-center gap-2">
                          <span className="w-20 shrink-0 text-2xs text-ink-muted">{label}</span>
                          <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-surface-raised">
                            <div
                              className={`h-full rounded-full ${colour}`}
                              style={{ width: `${(count / total) * 100}%` }}
                            />
                          </div>
                          <span className="tnum w-8 text-right text-2xs text-ink-muted">
                            {count}
                          </span>
                        </div>
                      )
                    })}
                  </div>
                </Card>
              </div>
            </div>

            {/* Recent captures */}
            <Card>
              <CardHeader
                title="Recent captures"
                actions={
                  <Button size="sm" variant="ghost" onClick={() => navigate('/capture')}>
                    All captures
                  </Button>
                }
              />
              <Table>
                <thead>
                  <tr>
                    <Th>Date</Th>
                    <Th>Operator</Th>
                    <Th>Floor</Th>
                    <Th>Zone</Th>
                    <Th className="text-right">Frames</Th>
                    <Th>Quality</Th>
                    <Th>Mapping</Th>
                    <Th>Status</Th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent_captures.map((capture) => (
                    <Tr key={capture.session_id}>
                      <Td>
                        <Link
                          to={`/capture/${capture.session_id}`}
                          className="font-medium text-ink hover:underline"
                        >
                          {formatDateTime(capture.captured_at)}
                        </Link>
                      </Td>
                      <Td className="text-xs text-ink-muted">{capture.operator_name ?? '—'}</Td>
                      <Td className="text-xs">{capture.floor_name}</Td>
                      <Td className="text-xs text-ink-muted">{capture.zone_name ?? '—'}</Td>
                      <Td className="tnum text-right">{capture.frame_count}</Td>
                      <Td>
                        <QualityBadge verdict={capture.quality_verdict} />
                      </Td>
                      <Td>
                        <MappingConfidenceBadge confidence={capture.mapping_confidence} />
                      </Td>
                      <Td>
                        <SessionStatusBadge status={capture.status} />
                      </Td>
                    </Tr>
                  ))}
                </tbody>
              </Table>
            </Card>

            {/* Attention */}
            <div className="grid gap-3 lg:grid-cols-2">
              <Card>
                <CardHeader
                  title="Areas needing attention"
                  description={`Capture points not visited within ${stale.data?.threshold_days ?? 14} days`}
                  actions={
                    <Badge tone={stale.data?.stale.length ? 'warn' : 'good'}>
                      {(stale.data?.stale.length ?? 0) + (stale.data?.never_captured.length ?? 0)}
                    </Badge>
                  }
                />
                {stale.isLoading ? (
                  <div className="space-y-2 p-4">
                    <Skeleton className="h-6" />
                    <Skeleton className="h-6" />
                  </div>
                ) : (stale.data?.stale.length ?? 0) + (stale.data?.never_captured.length ?? 0) ===
                  0 ? (
                  <EmptyState title="Every capture point is current" />
                ) : (
                  <ul className="divide-y divide-line">
                    {[...(stale.data?.stale ?? []), ...(stale.data?.never_captured ?? [])]
                      .slice(0, 12)
                      .map((item) => (
                        <li
                          key={item.location_id}
                          className="flex items-center gap-2 px-4 py-2 text-sm"
                        >
                          <Badge tone="neutral">{item.code}</Badge>
                          <span className="min-w-0 flex-1 truncate text-ink-muted">
                            {item.floor_name}
                            {item.zone_name ? ` · ${item.zone_name}` : ''}
                          </span>
                          <span className="tnum shrink-0 text-xs text-warn">
                            {item.days_since_capture === null
                              ? 'Never captured'
                              : `${item.days_since_capture} days`}
                          </span>
                        </li>
                      ))}
                  </ul>
                )}
              </Card>

              <Card>
                <CardHeader
                  title="Open annotations"
                  actions={
                    <Button size="sm" variant="ghost" onClick={() => navigate('/annotations')}>
                      Open log
                    </Button>
                  }
                />
                <div className="p-4">
                  <Metric
                    label="Unresolved findings"
                    value={formatNumber(data.annotation_open_count)}
                    tone={data.annotation_open_count ? 'warn' : 'good'}
                    provenance="manual"
                    hint="Recorded by site engineers — not detected automatically"
                  />
                  <Callout tone="info" className="mt-3" title="Defect detection is not implemented">
                    Findings are raised by people in the 360 viewer. Automatic defect detection is
                    a Phase 3 capability.
                  </Callout>
                  <div className="mt-3 flex gap-2">
                    <Button
                      size="sm"
                      variant="secondary"
                      icon={ClipboardList}
                      onClick={() => navigate('/annotations')}
                    >
                      Annotations
                    </Button>
                    <Button
                      size="sm"
                      variant="secondary"
                      icon={FileText}
                      onClick={() => navigate('/reports')}
                    >
                      Generate report
                    </Button>
                  </div>
                </div>
              </Card>
            </div>

            {data.capture_health.failed > 0 ? (
              <Callout tone="warn" title={`${data.capture_health.failed} capture(s) failed or were cancelled`}>
                <span className="flex items-center gap-1">
                  <TriangleAlert className="size-3" aria-hidden />
                  Open the capture to see the failing stage and retry it.
                </span>
              </Callout>
            ) : null}
          </>
        ) : null}
      </PageBody>
    </>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-2">
      <dt className="text-ink-muted">{label}</dt>
      <dd className="tnum truncate text-ink">{value}</dd>
    </div>
  )
}
