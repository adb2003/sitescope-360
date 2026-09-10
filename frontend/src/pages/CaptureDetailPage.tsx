/** Capture detail: live processing status, file metadata, frames, retry (§34, §45). */

import {
  AlertTriangle,
  CheckCircle2,
  CloudUpload,
  Images,
  Play,
  RefreshCw,
  RotateCcw,
} from 'lucide-react'
import { useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { PageBody, PageHeader } from '@/components/layout/AppShell'
import {
  JobStatusBadge,
  MappingConfidenceNote,
  Metric,
  QualityBadge,
  SessionStatusBadge,
  isSessionInFlight,
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
import {
  useCapture,
  useCaptureFiles,
  useCaptureFrames,
  useProcessCapture,
  useProcessingStatus,
  useRetryCapture,
  useUploadCaptureFiles,
} from '@/hooks/queries'
import { Capability, useAuth } from '@/hooks/useAuth'
import { ApiError } from '@/lib/api'
import { formatBytes, formatDateTime, formatDuration, humanize } from '@/lib/utils'
import { PIPELINE_ORDER } from '../../../shared/contracts'

export function CaptureDetailPage() {
  const { sessionId } = useParams<{ sessionId: string }>()
  const navigate = useNavigate()
  const { can } = useAuth()

  const session = useCapture(sessionId)
  const files = useCaptureFiles(sessionId)
  const status = useProcessingStatus(sessionId)
  const frames = useCaptureFrames(sessionId)
  const process = useProcessCapture(sessionId ?? '')
  const retry = useRetryCapture(sessionId ?? '')
  const upload = useUploadCaptureFiles(sessionId ?? '')
  const fileInput = useRef<HTMLInputElement>(null)
  const [error, setError] = useState<string | null>(null)

  const canUpload = can(Capability.Upload)
  const live = status.data
  const inFlight = live ? isSessionInFlight(live.status) : false

  if (session.isError) {
    return (
      <PageBody>
        <Card>
          <ErrorState message={session.error?.message} onRetry={() => void session.refetch()} />
        </Card>
      </PageBody>
    )
  }

  const stageByType = new Map((live?.stages ?? []).map((stage) => [stage.job_type, stage]))

  return (
    <>
      <PageHeader
        title={session.data?.name ?? 'Capture'}
        subtitle={
          session.data ? (
            <span className="font-mono text-xs">{session.data.id}</span>
          ) : (
            <Skeleton className="h-3 w-48" />
          )
        }
        breadcrumbs={[
          { label: 'Site capture', to: '/capture' },
          { label: session.data?.floor_name ?? '…' },
        ]}
        actions={
          <>
            {live?.status === 'failed' && canUpload ? (
              <Button
                variant="primary"
                icon={RotateCcw}
                loading={retry.isPending}
                onClick={() =>
                  retry.mutate(undefined, {
                    onError: (caught) =>
                      setError(caught instanceof ApiError ? caught.message : 'Retry failed'),
                  })
                }
              >
                Retry processing
              </Button>
            ) : null}
            {canUpload && live && !inFlight ? (
              <Button
                variant="secondary"
                icon={Play}
                loading={process.isPending}
                onClick={() =>
                  process.mutate(
                    { force: live.status === 'ready' },
                    {
                      onError: (caught) =>
                        setError(
                          caught instanceof ApiError ? caught.message : 'Could not start processing',
                        ),
                    },
                  )
                }
              >
                {live.status === 'ready' ? 'Reprocess' : 'Process'}
              </Button>
            ) : null}
            {frames.data?.items.length ? (
              <Button
                variant="primary"
                icon={Images}
                onClick={() => navigate(`/viewer/${frames.data.items[0]?.id}`)}
              >
                Open 360 viewer
              </Button>
            ) : null}
          </>
        }
      />

      <PageBody className="space-y-4">
        {error ? <Callout tone="bad">{error}</Callout> : null}

        {/* Processing status (§34) */}
        <Card>
          <CardHeader
            title="Processing"
            description="Each stage is independent and individually retryable"
            actions={
              <div className="flex items-center gap-2">
                {live ? <SessionStatusBadge status={live.status} /> : null}
                <Button
                  size="sm"
                  variant="ghost"
                  icon={RefreshCw}
                  onClick={() => void status.refetch()}
                >
                  Refresh
                </Button>
              </div>
            }
          />
          <div className="space-y-3 p-4">
            {status.isLoading ? (
              <Skeleton className="h-2" />
            ) : (
              <>
                <Progress
                  value={live?.progress ?? 0}
                  showLabel
                  tone={
                    live?.status === 'failed' ? 'bad' : live?.status === 'ready' ? 'good' : 'brand'
                  }
                />

                {live?.failure_reason ? (
                  <Callout tone="bad" title="Processing failed">
                    {live.failure_reason}
                  </Callout>
                ) : null}

                {/* The §34 stage ladder, in canonical order so a missing stage is visible. */}
                <ol className="grid gap-1.5 sm:grid-cols-2 lg:grid-cols-4">
                  {PIPELINE_ORDER.map((stageName) => {
                    const stage = stageByType.get(stageName)
                    return (
                      <li
                        key={stageName}
                        className="flex items-center gap-2 rounded border border-line bg-surface-raised px-2.5 py-1.5"
                      >
                        <span className="min-w-0 flex-1 truncate text-xs text-ink">
                          {humanize(stageName)}
                        </span>
                        {stage ? (
                          <JobStatusBadge status={stage.status} />
                        ) : (
                          <Badge tone="neutral">Not run</Badge>
                        )}
                      </li>
                    )
                  })}
                </ol>

                {live?.stages.length ? (
                  <Table>
                    <thead>
                      <tr>
                        <Th>Stage</Th>
                        <Th>Status</Th>
                        <Th className="text-right">Attempt</Th>
                        <Th className="text-right">Duration</Th>
                        <Th>Detail</Th>
                      </tr>
                    </thead>
                    <tbody>
                      {live.stages.map((stage) => (
                        <Tr key={stage.id}>
                          <Td className="font-medium">{humanize(stage.job_type)}</Td>
                          <Td>
                            <JobStatusBadge status={stage.status} />
                          </Td>
                          <Td className="tnum text-right">
                            {stage.attempt}/{stage.max_attempts}
                          </Td>
                          <Td className="tnum text-right text-xs">
                            {formatDuration(stage.duration_seconds)}
                          </Td>
                          <Td className="max-w-sm truncate text-xs text-ink-muted">
                            {stage.error_detail ?? stage.message ?? summariseResult(stage.result)}
                          </Td>
                        </Tr>
                      ))}
                    </tbody>
                  </Table>
                ) : null}
              </>
            )}
          </div>
        </Card>

        {session.data ? (
          <MappingConfidenceNote
            confidence={session.data.mapping_confidence}
            note={session.data.mapping_note}
          />
        ) : null}

        {/* Session facts */}
        <div className="grid gap-3 lg:grid-cols-4">
          <Card className="p-3.5">
            <Metric
              label="Files"
              value={session.data?.file_count ?? '—'}
              provenance="measured"
            />
          </Card>
          <Card className="p-3.5">
            <Metric
              label="Frames"
              value={session.data?.frame_count ?? '—'}
              provenance="measured"
            />
          </Card>
          <Card className="p-3.5">
            <Metric
              label="Placed on plan"
              value={frames.data?.items.filter((frame) => frame.location_id).length ?? '—'}
              provenance="manual"
              hint={`of ${frames.data?.total ?? 0} frames`}
            />
          </Card>
          <Card className="p-3.5">
            <Metric
              label="Started"
              value={formatDateTime(session.data?.started_at)}
              provenance="measured"
              hint={session.data?.operator_name ?? undefined}
            />
          </Card>
        </div>

        {/* Uploaded files (§7 metadata display) */}
        <Card>
          <CardHeader
            title="Uploaded files"
            description="Filename, size, dimensions, timestamp, camera and GPS where available"
            actions={
              canUpload ? (
                <>
                  <input
                    ref={fileInput}
                    type="file"
                    multiple
                    accept=".jpg,.jpeg,.png,.mp4,.mov"
                    className="hidden"
                    onChange={(event) => {
                      const chosen = Array.from(event.target.files ?? [])
                      if (!chosen.length) return
                      setError(null)
                      upload.mutate(chosen, {
                        onError: (caught) =>
                          setError(
                            caught instanceof ApiError ? caught.message : 'Upload failed',
                          ),
                      })
                      event.target.value = ''
                    }}
                  />
                  <Button
                    size="sm"
                    variant="secondary"
                    icon={CloudUpload}
                    loading={upload.isPending}
                    onClick={() => fileInput.current?.click()}
                  >
                    Add files
                  </Button>
                </>
              ) : null
            }
          />
          {files.isLoading ? (
            <div className="space-y-2 p-4">
              <Skeleton className="h-8" />
              <Skeleton className="h-8" />
            </div>
          ) : !files.data?.length ? (
            <EmptyState
              icon={CloudUpload}
              title="No files yet"
              description="Add the 360 images or video recorded on site."
            />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>File</Th>
                  <Th>Kind</Th>
                  <Th>Projection</Th>
                  <Th className="text-right">Size</Th>
                  <Th className="text-right">Dimensions</Th>
                  <Th className="text-right">Duration</Th>
                  <Th>Camera</Th>
                  <Th>GPS</Th>
                  <Th className="text-right">Frames</Th>
                  <Th>Checks</Th>
                </tr>
              </thead>
              <tbody>
                {files.data.map((file) => (
                  <Tr key={file.id}>
                    <Td className="max-w-[200px] truncate font-medium">
                      {file.original_filename}
                    </Td>
                    <Td>
                      <Badge
                        tone={
                          file.kind === 'panorama' || file.kind === 'video_360' ? 'brand' : 'warn'
                        }
                      >
                        {humanize(file.kind)}
                      </Badge>
                    </Td>
                    <Td className="text-xs text-ink-muted">{humanize(file.projection)}</Td>
                    <Td className="tnum text-right text-xs">{formatBytes(file.file_size_bytes)}</Td>
                    <Td className="tnum text-right text-xs">
                      {file.width_px && file.height_px
                        ? `${file.width_px}×${file.height_px}`
                        : '—'}
                    </Td>
                    <Td className="tnum text-right text-xs">
                      {file.duration_seconds
                        ? `${file.duration_seconds.toFixed(1)}s @ ${file.fps?.toFixed(0) ?? '?'}fps`
                        : '—'}
                    </Td>
                    <Td className="max-w-[140px] truncate text-xs text-ink-muted">
                      {[file.camera_make, file.camera_model].filter(Boolean).join(' ') || '—'}
                    </Td>
                    <Td className="tnum text-xs text-ink-muted">
                      {file.gps_latitude !== null && file.gps_longitude !== null
                        ? `${file.gps_latitude.toFixed(4)}, ${file.gps_longitude.toFixed(4)}`
                        : '—'}
                    </Td>
                    <Td className="tnum text-right">{file.frame_count}</Td>
                    <Td>
                      {file.validation_errors?.length ? (
                        <Badge tone="bad" icon={AlertTriangle}>
                          {file.validation_errors.length} error
                        </Badge>
                      ) : file.validation_warnings?.length ? (
                        <Badge
                          tone="warn"
                          icon={AlertTriangle}
                          title={file.validation_warnings.join(' • ')}
                        >
                          {file.validation_warnings.length} warning
                        </Badge>
                      ) : (
                        <Badge tone="good" icon={CheckCircle2}>
                          OK
                        </Badge>
                      )}
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}

          {files.data?.some((file) => file.validation_warnings?.length) ? (
            <div className="space-y-1.5 border-t border-line p-4">
              {files.data
                .filter((file) => file.validation_warnings?.length)
                .map((file) => (
                  <Callout key={file.id} tone="warn" title={file.original_filename}>
                    <ul className="list-inside list-disc space-y-0.5">
                      {file.validation_warnings?.map((warning) => (
                        <li key={warning}>{warning}</li>
                      ))}
                    </ul>
                  </Callout>
                ))}
            </div>
          ) : null}
        </Card>

        {/* Frames */}
        {frames.data?.items.length ? (
          <Card>
            <CardHeader
              title="Frames"
              description="Each frame is a viewable panorama and the unit the timeline and compare view address"
              actions={<Badge tone="neutral">{frames.data.total}</Badge>}
            />
            <div className="grid grid-cols-2 gap-2 p-3 sm:grid-cols-4 lg:grid-cols-6 xl:grid-cols-8">
              {frames.data.items.slice(0, 48).map((frame) => (
                <button
                  key={frame.id}
                  type="button"
                  onClick={() => navigate(`/viewer/${frame.id}`)}
                  className="group overflow-hidden rounded border border-line text-left transition-colors hover:border-brand"
                >
                  {frame.thumbnail?.url ? (
                    <img
                      src={frame.thumbnail.url}
                      alt=""
                      loading="lazy"
                      className="h-16 w-full object-cover"
                    />
                  ) : (
                    <div className="h-16 w-full bg-surface-raised" />
                  )}
                  <div className="flex items-center justify-between gap-1 px-1.5 py-1">
                    <span className="truncate text-2xs font-medium text-ink">
                      {frame.location_code ?? `#${frame.sequence_index + 1}`}
                    </span>
                    <QualityBadge verdict={frame.quality?.verdict} />
                  </div>
                </button>
              ))}
            </div>
            {frames.data.total > 48 ? (
              <p className="border-t border-line px-4 py-2 text-2xs text-ink-subtle">
                Showing the first 48 of {frames.data.total} frames. Open the viewer to browse them
                all.
              </p>
            ) : null}
          </Card>
        ) : null}
      </PageBody>
    </>
  )
}

/** Turn a stage result blob into one readable line for the table. */
function summariseResult(result: Record<string, unknown> | null): string {
  if (!result) return '—'
  const interesting = [
    'frame_count',
    'assessed',
    'keyframes',
    'total_features',
    'confidence',
    'path_steps',
    'failed_count',
  ]
  const parts = interesting
    .filter((key) => key in result)
    .map((key) => `${key.replace(/_/g, ' ')}: ${String(result[key])}`)
  return parts.length ? parts.join(', ') : '—'
}