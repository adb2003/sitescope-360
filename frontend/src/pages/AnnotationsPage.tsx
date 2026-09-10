/** Annotation log (§28) — the basis of defect management, recorded by people. */

import { ClipboardList, ExternalLink, Filter } from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { PageBody, PageHeader } from '@/components/layout/AppShell'
import { Metric } from '@/components/status'
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
  Table,
  Td,
  Th,
  Tr,
} from '@/components/ui'
import { useProjectAnnotations, useProjectFloors, useUpdateAnnotation } from '@/hooks/queries'
import { Capability, useAuth } from '@/hooks/useAuth'
import { useProjectContext } from '@/hooks/useProjectContext'
import { formatDateTime, humanize } from '@/lib/utils'
import {
  ANNOTATION_PRIORITY_VALUES,
  ANNOTATION_STATUS_VALUES,
  type AnnotationStatus,
} from '../../../shared/contracts'

export function AnnotationsPage() {
  const { projectId } = useProjectContext()
  const { can } = useAuth()
  const navigate = useNavigate()
  const floors = useProjectFloors(projectId)

  const [status, setStatus] = useState('')
  const [priority, setPriority] = useState('')
  const [floorId, setFloorId] = useState('')

  const filters = {
    ...(status ? { status } : {}),
    ...(priority ? { priority } : {}),
    ...(floorId ? { floor_id: floorId } : {}),
  }
  const annotations = useProjectAnnotations(projectId, filters)
  const update = useUpdateAnnotation()
  const canResolve = can(Capability.AnnotationResolve)

  if (!projectId) {
    return (
      <PageBody>
        <Card>
          <EmptyState
            icon={ClipboardList}
            title="No project selected"
            description="Choose a project from the sidebar to see its annotation log."
          />
        </Card>
      </PageBody>
    )
  }

  const items = annotations.data?.items ?? []
  const open = items.filter((item) => item.status !== 'resolved')
  const critical = open.filter((item) => item.priority === 'critical')

  return (
    <>
      <PageHeader
        title="Annotations"
        subtitle="Findings pinned inside 360 captures"
        actions={
          <>
            <Select
              value={floorId}
              onChange={(event) => setFloorId(event.target.value)}
              className="w-40"
              aria-label="Floor"
            >
              <option value="">All floors</option>
              {floors.data?.map((floor) => (
                <option key={floor.id} value={floor.id}>
                  {floor.name}
                </option>
              ))}
            </Select>
            <Select
              value={status}
              onChange={(event) => setStatus(event.target.value)}
              className="w-36"
              aria-label="Status"
            >
              <option value="">Any status</option>
              {ANNOTATION_STATUS_VALUES.map((value) => (
                <option key={value} value={value}>
                  {humanize(value)}
                </option>
              ))}
            </Select>
            <Select
              value={priority}
              onChange={(event) => setPriority(event.target.value)}
              className="w-36"
              aria-label="Priority"
            >
              <option value="">Any priority</option>
              {ANNOTATION_PRIORITY_VALUES.map((value) => (
                <option key={value} value={value}>
                  {humanize(value)}
                </option>
              ))}
            </Select>
          </>
        }
      />

      <PageBody className="space-y-4">
        {annotations.isError ? (
          <Card>
            <ErrorState
              message={annotations.error?.message}
              onRetry={() => void annotations.refetch()}
            />
          </Card>
        ) : null}

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Card className="p-3.5">
            <Metric label="Total" value={annotations.data?.total ?? '—'} provenance="manual" />
          </Card>
          <Card className="p-3.5">
            <Metric
              label="Open"
              value={open.length}
              tone={open.length ? 'warn' : 'good'}
              provenance="manual"
            />
          </Card>
          <Card className="p-3.5">
            <Metric
              label="Critical open"
              value={critical.length}
              tone={critical.length ? 'bad' : 'good'}
              provenance="manual"
            />
          </Card>
          <Card className="p-3.5">
            <Metric
              label="Resolved"
              value={items.length - open.length}
              tone="good"
              provenance="manual"
            />
          </Card>
        </div>

        <Callout tone="info" title="Recorded manually, by design">
          Every finding here was raised by a person in the 360 viewer. The platform does not detect
          defects automatically — that is a Phase 3 capability, and inventing findings would be
          worse than having none.
        </Callout>

        <Card>
          <CardHeader
            title="Findings"
            description={
              Object.keys(filters).length ? 'Filtered' : 'Every annotation in this project'
            }
            actions={
              Object.keys(filters).length ? (
                <Button
                  size="sm"
                  variant="ghost"
                  icon={Filter}
                  onClick={() => {
                    setStatus('')
                    setPriority('')
                    setFloorId('')
                  }}
                >
                  Clear filters
                </Button>
              ) : null
            }
          />

          {annotations.isLoading ? (
            <div className="space-y-2 p-4">
              {[0, 1, 2].map((index) => (
                <Skeleton key={index} className="h-9" />
              ))}
            </div>
          ) : !items.length ? (
            <EmptyState
              icon={ClipboardList}
              title="No findings"
              description="Open a capture in the 360 viewer, use the annotation tool, and click the panorama to pin a finding."
            />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Finding</Th>
                  <Th>Priority</Th>
                  <Th>Status</Th>
                  <Th>Floor</Th>
                  <Th>Point</Th>
                  <Th>Raised by</Th>
                  <Th>Raised</Th>
                  <Th className="w-28" />
                </tr>
              </thead>
              <tbody>
                {items.map((annotation) => (
                  <Tr key={annotation.id}>
                    <Td className="max-w-sm">
                      <p className="truncate font-medium text-ink">{annotation.title}</p>
                      {annotation.description ? (
                        <p className="truncate text-2xs text-ink-muted">
                          {annotation.description}
                        </p>
                      ) : null}
                    </Td>
                    <Td>
                      <Badge
                        tone={
                          annotation.priority === 'critical'
                            ? 'bad'
                            : annotation.priority === 'high'
                              ? 'warn'
                              : 'neutral'
                        }
                      >
                        {humanize(annotation.priority)}
                      </Badge>
                    </Td>
                    <Td>
                      {canResolve ? (
                        <Select
                          value={annotation.status}
                          onChange={(event) =>
                            update.mutate({
                              id: annotation.id,
                              status: event.target.value as AnnotationStatus,
                            })
                          }
                          className="h-7 py-0 text-xs"
                          aria-label={`Status of ${annotation.title}`}
                        >
                          {ANNOTATION_STATUS_VALUES.map((value) => (
                            <option key={value} value={value}>
                              {humanize(value)}
                            </option>
                          ))}
                        </Select>
                      ) : (
                        <Badge tone={annotation.status === 'resolved' ? 'good' : 'warn'}>
                          {humanize(annotation.status)}
                        </Badge>
                      )}
                    </Td>
                    <Td className="text-xs text-ink-muted">{annotation.floor_name ?? '—'}</Td>
                    <Td>
                      {annotation.location_code ? (
                        <Badge tone="brand">{annotation.location_code}</Badge>
                      ) : (
                        <span className="text-2xs text-ink-subtle">Unplaced</span>
                      )}
                    </Td>
                    <Td className="text-xs text-ink-muted">
                      {annotation.created_by_name ?? '—'}
                    </Td>
                    <Td className="text-xs text-ink-muted">
                      {formatDateTime(annotation.created_at)}
                    </Td>
                    <Td>
                      <Button
                        size="sm"
                        variant="ghost"
                        icon={ExternalLink}
                        onClick={() => navigate(`/viewer/${annotation.frame_id}`)}
                      >
                        View
                      </Button>
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>
      </PageBody>
    </>
  )
}
