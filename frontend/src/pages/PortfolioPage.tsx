/** Cross-project overview — the top-level dashboard (§21). */

import { AlertTriangle, ArrowRight, Building2, Camera, Images } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'

import { PageBody, PageHeader } from '@/components/layout/AppShell'
import { FrequencyBadge, Metric } from '@/components/status'
import {
  Button,
  Card,
  CardHeader,
  Callout,
  EmptyState,
  ErrorState,
  Progress,
  Skeleton,
  Table,
  Td,
  Th,
  Tr,
} from '@/components/ui'
import { usePortfolio } from '@/hooks/queries'
import { useProjectContext } from '@/hooks/useProjectContext'
import { formatNumber, formatRelative, humanize } from '@/lib/utils'

export function PortfolioPage() {
  const { data, isLoading, isError, error, refetch } = usePortfolio()
  const { setProjectId } = useProjectContext()
  const navigate = useNavigate()

  function open(projectId: string) {
    setProjectId(projectId)
    navigate(`/projects/${projectId}`)
  }

  return (
    <>
      <PageHeader
        title="Dashboard"
        subtitle="Capture activity and coverage across every project"
        actions={
          <Button variant="secondary" onClick={() => navigate('/projects')}>
            Manage projects
          </Button>
        }
      />

      <PageBody className="space-y-4">
        {isError ? (
          <Card>
            <ErrorState message={error?.message} onRetry={() => void refetch()} />
          </Card>
        ) : null}

        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {isLoading ? (
            <>
              {[0, 1, 2, 3].map((index) => (
                <Card key={index} className="p-4">
                  <Skeleton className="h-3 w-20" />
                  <Skeleton className="mt-2 h-7 w-16" />
                </Card>
              ))}
            </>
          ) : (
            <>
              <Card className="p-4">
                <Metric
                  label="Projects"
                  value={formatNumber(data?.total_projects)}
                  provenance="measured"
                />
              </Card>
              <Card className="p-4">
                <Metric
                  label="Capture sessions"
                  value={formatNumber(data?.total_sessions)}
                  provenance="measured"
                />
              </Card>
              <Card className="p-4">
                <Metric
                  label="360 frames"
                  value={formatNumber(data?.total_frames)}
                  provenance="measured"
                />
              </Card>
              <Card className="p-4">
                <Metric
                  label="Overdue projects"
                  value={formatNumber(data?.overdue_count)}
                  tone={data?.overdue_count ? 'bad' : 'good'}
                  provenance="measured"
                  hint="Against the configured cadence"
                />
              </Card>
            </>
          )}
        </div>

        <Card>
          <CardHeader
            title="Projects"
            description="Coverage is measured against administrator-declared capture points, not inferred from imagery."
          />
          {isLoading ? (
            <div className="space-y-2 p-4">
              {[0, 1, 2].map((index) => (
                <Skeleton key={index} className="h-9" />
              ))}
            </div>
          ) : !data?.projects.length ? (
            <EmptyState
              icon={Building2}
              title="No projects yet"
              description="Create a project, add a building and a floor, then upload your first 360 capture."
              action={
                <Button variant="primary" onClick={() => navigate('/projects')}>
                  Create a project
                </Button>
              }
            />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Project</Th>
                  <Th>Status</Th>
                  <Th className="text-right">Floors</Th>
                  <Th className="text-right">Captures</Th>
                  <Th className="w-40">Coverage</Th>
                  <Th>Last capture</Th>
                  <Th>Schedule</Th>
                  <Th className="text-right">Open issues</Th>
                  <Th className="w-8" />
                </tr>
              </thead>
              <tbody>
                {data.projects.map((row) => (
                  <Tr key={row.project_id} className="cursor-pointer" onClick={() => open(row.project_id)}>
                    <Td>
                      <p className="font-medium text-ink">{row.project_name}</p>
                      <p className="font-mono text-2xs text-ink-subtle">{row.project_code}</p>
                    </Td>
                    <Td className="text-xs text-ink-muted">{humanize(row.status)}</Td>
                    <Td className="tnum text-right">{row.floor_count}</Td>
                    <Td className="tnum text-right">{row.session_count}</Td>
                    <Td>
                      <Progress
                        value={row.coverage_ratio}
                        showLabel
                        tone={
                          row.coverage_ratio >= 0.8
                            ? 'good'
                            : row.coverage_ratio >= 0.4
                              ? 'warn'
                              : 'bad'
                        }
                      />
                    </Td>
                    <Td className="text-xs text-ink-muted">{formatRelative(row.last_capture_at)}</Td>
                    <Td>
                      <FrequencyBadge status={row.frequency_status} />
                    </Td>
                    <Td className="tnum text-right">
                      {row.open_annotations ? (
                        <span className="text-warn">{row.open_annotations}</span>
                      ) : (
                        <span className="text-ink-subtle">0</span>
                      )}
                    </Td>
                    <Td>
                      <ArrowRight className="size-3.5 text-ink-subtle" aria-hidden />
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>

        {data?.overdue_count ? (
          <Callout tone="warn" title={`${data.overdue_count} project(s) overdue for capture`}>
            A project is overdue when the interval implied by its configured captures-per-week,
            plus its grace period, has elapsed since the last completed capture.
          </Callout>
        ) : null}

        <div className="grid gap-3 lg:grid-cols-2">
          <Card>
            <CardHeader title="Getting started" />
            <ol className="space-y-2 p-4 text-sm text-ink-muted">
              {[
                ['Create a project', 'Add a building, floors and zones under Projects.'],
                ['Upload a floor plan', 'A PNG or JPG export of the drawing, under Floor Plans.'],
                ['Capture the site', 'Record on any 360 camera, then upload under Site Capture.'],
                ['Place the captures', 'Drop capture points on the plan and bind frames to them.'],
                ['Compare and report', 'Revisit, compare dates, and export a report.'],
              ].map(([title, detail], index) => (
                <li key={title} className="flex gap-2.5">
                  <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-brand-soft text-2xs font-bold text-brand">
                    {index + 1}
                  </span>
                  <span>
                    <span className="font-medium text-ink">{title}</span> — {detail}
                  </span>
                </li>
              ))}
            </ol>
          </Card>

          <Card>
            <CardHeader
              title="What this platform does not claim"
              description="Stated up front rather than discovered later."
            />
            <div className="space-y-2 p-4">
              <Callout tone="info" title="Automatic positioning is relative">
                Visual positioning recovers the shape of a walkthrough, not metric coordinates. It
                reports a confidence and asks for manual placement when that confidence is low.
              </Callout>
              <Callout tone="info" title="Coverage is declared, not detected">
                Coverage compares visited capture points against the points an administrator
                expects per zone. Square footage is apportioned from that ratio.
              </Callout>
              <Callout tone="info" title="No progress or defect AI">
                Progress detection, defect detection and BIM comparison are not implemented.
                Defects are recorded manually as annotations.
              </Callout>
              <p className="flex items-center gap-1.5 pt-1 text-2xs text-ink-subtle">
                <AlertTriangle className="size-3" aria-hidden />
                See <Link to="/settings" className="underline hover:text-ink">Settings → Capabilities</Link>{' '}
                for the machine-readable list.
              </p>
            </div>
          </Card>
        </div>

        <div className="flex flex-wrap gap-2">
          <Button variant="secondary" icon={Camera} onClick={() => navigate('/capture')}>
            Site capture
          </Button>
          <Button variant="secondary" icon={Images} onClick={() => navigate('/projects')}>
            Projects
          </Button>
        </div>
      </PageBody>
    </>
  )
}
