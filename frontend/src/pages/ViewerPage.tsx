/** The 360 viewer page (§11): panorama, filmstrip, quality, annotations, plan placement. */

import { ChevronLeft, ChevronRight, Images, MapPin, Columns2 } from 'lucide-react'
import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { useNavigate, useParams } from 'react-router-dom'

import { PageBody, PageHeader } from '@/components/layout/AppShell'
import { FloorPlanCanvas } from '@/components/floorplan/FloorPlanCanvas'
import { MappingConfidenceNote } from '@/components/status'
import { PlanMinimap, type MinimapOrientation } from '@/components/viewer/PlanMinimap'
import { AnchorPlacement, type PendingAnchor } from '@/components/viewer/AnchorPlacement'
import { RecognisePlacement } from '@/components/viewer/RecognisePlacement'
import { ShareFrame } from '@/components/viewer/ShareFrame'
import { TimeTravelPanel } from '@/components/viewer/TimeTravelPanel'
import { FrameFilmstrip, QualityPanel, Viewer360 } from '@/components/viewer/Viewer360'
import {
  Badge,
  Button,
  Callout,
  Card,
  CardHeader,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  Input,
  Select,
  Skeleton,
  Tabs,
  Textarea,
} from '@/components/ui'
import {
  useAssignFrameLocation,
  useCapture,
  useCaptureFrames,
  useCaptures,
  useAlignCapture,
  useCapturesNear,
  useCreateAnnotation,
  useFloorPaths,
  useFloorPlan,
  useClearPlacement,
  useFrameAnnotations,
  useLocalizeCapture,
  useLocations,
  useViewerManifest,
} from '@/hooks/queries'
import { Capability } from '@/hooks/useAuth'
import { useProjectCan } from '@/hooks/useProjectCan'
import { useProjectContext } from '@/hooks/useProjectContext'
import { ApiError } from '@/lib/api'
import type { AlignTrackResult, ClearPlacementResult, LocalizeResult } from '@/types/api'
import { formatDateTime, humanize } from '@/lib/utils'
import {
  ANNOTATION_PRIORITY_VALUES,
} from '../../../shared/contracts'

/**
 * How far from a click to look for capture points, in plan pixels.
 *
 * A radius rather than an exact hit, because nobody clicks a capture point dead on. This is
 * roughly the spacing between adjacent points on the demo floors, so it reaches the one the
 * operator meant without sweeping in its neighbours — the number to revisit first if a click
 * starts returning three points where it should return one.
 */
const PROBE_RADIUS_PX = 60

export function ViewerPage() {
  const params = useParams<{ frameId: string }>()
  const navigate = useNavigate()
  const { projectId } = useProjectContext()
  const { can } = useProjectCan(projectId)

  const [frameId, setFrameId] = useState<string | null>(params.frameId ?? null)
  // Mirrored from the viewer so the minimap's cone follows where the operator is looking.
  const [viewerOrientation, setViewerOrientation] = useState<MinimapOrientation | undefined>()
  const [panel, setPanel] = useState<'quality' | 'plan' | 'history' | 'issues'>('quality')

  /** Where the operator last clicked on the plan. Null until they have. */
  const [probe, setProbe] = useState<{ x: number; y: number } | null>(null)
  const [pendingAnnotation, setPendingAnnotation] = useState<{ yaw: number; pitch: number } | null>(
    null,
  )
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    if (params.frameId && params.frameId !== frameId) setFrameId(params.frameId)
  }, [params.frameId, frameId])

  // With no frame in the URL, open the newest ready capture's first frame.
  const captures = useCaptures(
    projectId ? { project_id: projectId, status: 'ready' } : {},
  )
  const newestSessionId = captures.data?.items[0]?.id
  const fallbackFrames = useCaptureFrames(frameId ? undefined : newestSessionId)

  useEffect(() => {
    if (!frameId && fallbackFrames.data?.items.length) {
      setFrameId(fallbackFrames.data.items[0]?.id ?? null)
    }
  }, [frameId, fallbackFrames.data])

  const manifest = useViewerManifest(frameId ?? undefined)
  const frame = manifest.data?.frame
  const session = useCapture(frame?.session_id)
  const sessionFrames = useCaptureFrames(frame?.session_id)
  const annotations = useFrameAnnotations(frameId ?? undefined)
  const plan = useFloorPlan(frame?.floor_id ?? undefined)
  const locations = useLocations(frame?.floor_id ?? undefined)
  const paths = useFloorPaths(frame?.floor_id ?? undefined)
  // Declared here rather than beside the `probe` state because it needs the floor, and the floor
  // is only known once the frame has loaded.
  const nearby = useCapturesNear(frame?.floor_id ?? undefined, probe, PROBE_RADIUS_PX)

  // Anchors the operator has dropped but not yet submitted, and the pick mode that collects them.
  const [anchors, setAnchors] = useState<PendingAnchor[]>([])
  const [picking, setPicking] = useState(false)
  const [alignResult, setAlignResult] = useState<AlignTrackResult | null>(null)
  const [alignAnchorCount, setAlignAnchorCount] = useState<number | null>(null)
  const [alignError, setAlignError] = useState<string | null>(null)
  const align = useAlignCapture(frame?.session_id ?? '', frame?.floor_id ?? undefined)
  const [localizeResult, setLocalizeResult] = useState<LocalizeResult | null>(null)
  const [clearedPlacement, setClearedPlacement] = useState<ClearPlacementResult | null>(null)
  const [localizeError, setLocalizeError] = useState<string | null>(null)
  const localize = useLocalizeCapture(frame?.session_id ?? '', frame?.floor_id ?? undefined)
  const clearPlacement = useClearPlacement(frame?.session_id ?? '', frame?.floor_id ?? undefined)
  const assign = useAssignFrameLocation(frame?.floor_id ?? undefined)

  const canAnnotate = can(Capability.Annotate)
  const canPlace = can(Capability.LocationEdit)

  const select = (nextFrameId: string) => {
    setFrameId(nextFrameId)
    navigate(`/viewer/${nextFrameId}`, { replace: true })
  }

  const orderedFrames = useMemo(() => sessionFrames.data?.items ?? [], [sessionFrames.data])

  if (!projectId) {
    return (
      <PageBody>
        <Card>
          <EmptyState
            icon={Images}
            title="No project selected"
            description="Choose a project from the sidebar to browse its captures."
          />
        </Card>
      </PageBody>
    )
  }

  if (manifest.isError) {
    return (
      <PageBody>
        <Card>
          <ErrorState message={manifest.error?.message} onRetry={() => void manifest.refetch()} />
        </Card>
      </PageBody>
    )
  }

  if (!frameId || manifest.isLoading) {
    return (
      <>
        <PageHeader title="360 Viewer" />
        <PageBody>
          {captures.isLoading || manifest.isLoading || fallbackFrames.isLoading ? (
            <Card className="p-4">
              <Skeleton className="h-96" />
            </Card>
          ) : (
            <Card>
              <EmptyState
                icon={Images}
                title="No processed captures yet"
                description="Upload a 360 image or video and run it through processing to view it here."
                action={<Button onClick={() => navigate('/capture')}>Go to site capture</Button>}
              />
            </Card>
          )}
        </PageBody>
      </>
    )
  }

  return (
    <>
      <PageHeader
        title="360 Viewer"
        subtitle={
          frame ? (
            <span>
              {frame.floor_name}
              {frame.location_code ? ` · ${frame.location_code}` : ''} ·{' '}
              {formatDateTime(frame.captured_at)}
            </span>
          ) : null
        }
        actions={
          <>
            <Button
              variant="secondary"
              size="sm"
              icon={ChevronLeft}
              disabled={!manifest.data?.previous_frame_id}
              onClick={() =>
                manifest.data?.previous_frame_id && select(manifest.data.previous_frame_id)
              }
            >
              Previous
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={!manifest.data?.next_frame_id}
              onClick={() => manifest.data?.next_frame_id && select(manifest.data.next_frame_id)}
            >
              Next
              <ChevronRight aria-hidden />
            </Button>
            {frame?.location_id ? (
              <Button
                variant="secondary"
                size="sm"
                icon={Columns2}
                onClick={() => navigate(`/compare?location=${frame.location_id}`)}
              >
                Compare dates
              </Button>
            ) : null}
          </>
        }
      />

      <PageBody className="space-y-3">
        {notice ? <Callout tone="info">{notice}</Callout> : null}

        {session.data ? (
          <MappingConfidenceNote
            confidence={session.data.mapping_confidence}
            note={session.data.mapping_note}
            method={session.data.mapping_method}
            loopClosure={session.data.loop_closure_report}
          />
        ) : null}

        <div className="grid gap-3 xl:grid-cols-[minmax(0,1fr)_340px]">
          {/* min-w-0 because a grid item defaults to min-width:auto, so it grows to fit its
              widest child rather than shrinking. The frame filmstrip already asks for
              overflow-x-auto, but it can only scroll inside a parent that is allowed to be
              narrower than its contents -- without this the strip stretched the column to 2400px
              on a 31-frame capture and gave the whole page a horizontal scrollbar. */}
          <div className="min-w-0 space-y-3">
            {manifest.data ? (
              // Positioned so the minimap floats over the panorama, the way it does on site:
              // where you are and what you are looking at belong on one screen, not two.
              <div className="relative">
                <Viewer360
                  manifest={manifest.data}
                  annotations={annotations.data ?? []}
                  onSelectFrame={select}
                  onPlaceAnnotation={
                    canAnnotate ? (yaw, pitch) => setPendingAnnotation({ yaw, pitch }) : undefined
                  }
                  onSelectAnnotation={(annotation) => {
                    setPanel('issues')
                    setNotice(`${annotation.title} — ${humanize(annotation.status)}`)
                  }}
                  onOrientationChange={(next) =>
                    setViewerOrientation({ yaw: next.yaw, fov: next.fov })
                  }
                  className="h-[62vh] min-h-[420px]"
                />

                {plan.data ? (
                  <div className="pointer-events-none absolute right-3 top-3 z-10 flex justify-end">
                    <PlanMinimap
                      plan={plan.data}
                      locations={locations.data ?? []}
                      frames={orderedFrames}
                      activeFrameId={frameId}
                      orientation={viewerOrientation}
                      onSelectFrame={select}
                      onProbe={(point) => {
                        setProbe(point)
                        setPanel('history')
                      }}
                    />
                  </div>
                ) : null}
              </div>
            ) : null}

            {orderedFrames.length > 1 ? (
              <FrameFilmstrip
                frames={orderedFrames}
                activeFrameId={frameId}
                onSelect={select}
              />
            ) : null}
          </div>

          <div className="space-y-3">
            <Card>
              <div className="border-b border-line p-2">
                <Tabs
                  value={panel}
                  onChange={setPanel}
                  options={[
                    { value: 'quality', label: 'Quality' },
                    { value: 'plan', label: 'Plan' },
                    {
                      value: 'history',
                      label: 'History',
                      count: nearby.data?.[0]?.visits.length,
                    },
                    { value: 'issues', label: 'Issues', count: annotations.data?.length },
                  ]}
                />
              </div>

              <div className="p-4">
                {panel === 'quality' && frame ? <QualityPanel frame={frame} /> : null}

                {panel === 'history' ? (
                  <TimeTravelPanel
                    point={probe}
                    results={nearby.data}
                    loading={nearby.isFetching}
                    radiusPx={PROBE_RADIUS_PX}
                    activeFrameId={frameId}
                    onSelectFrame={select}
                    onCompare={(a, b) => navigate(`/compare?frame_a=${a}&frame_b=${b}`)}
                  />
                ) : null}

                {panel === 'plan' ? (
                  plan.data ? (
                    <div className="space-y-2">
                      <FloorPlanCanvas
                        plan={plan.data}
                        locations={locations.data ?? []}
                        paths={paths.data?.items ?? []}
                        selectedLocationId={frame?.location_id ?? null}
                        editable={false}
                        onSelectLocation={(location) => {
                          if (location.latest_frame_id) select(location.latest_frame_id)
                        }}
                        pickMode={picking}
                        onPickPoint={(x, y) => {
                          // One anchor per frame: clicking again moves it rather than adding a
                          // second claim about the same frame, which the fit cannot satisfy.
                          setAnchors((current) => [
                            ...current.filter((anchor) => anchor.frameId !== frameId),
                            { frameId, x, y },
                          ])
                          setPicking(false)
                          setAlignResult(null)
                          setAlignError(null)
                        }}
                        markers={anchors.map((anchor, index) => ({
                          id: anchor.frameId,
                          x: anchor.x,
                          y: anchor.y,
                          label: String(index + 1),
                          title: `Anchor ${index + 1} — not yet placed`,
                        }))}
                        className="h-64"
                      />

                      {canPlace && orderedFrames.length >= 2 ? (
                        <AnchorPlacement
                          frames={orderedFrames}
                          activeFrameId={frameId}
                          anchors={anchors}
                          picking={picking}
                          pending={align.isPending}
                          result={alignResult}
                          anchorsUsed={alignAnchorCount}
                          error={alignError}
                          onTogglePicking={() => setPicking((value) => !value)}
                          onRemove={(id) =>
                            setAnchors((current) =>
                              current.filter((anchor) => anchor.frameId !== id),
                            )
                          }
                          onSubmit={() => {
                            setAlignError(null)
                            align.mutate(
                              anchors.map((anchor) => ({
                                frame_id: anchor.frameId,
                                x: anchor.x,
                                y: anchor.y,
                              })),
                              {
                                onSuccess: (result) => {
                                  setAlignResult(result)
                                  setAlignAnchorCount(anchors.length)
                                  setAnchors([])
                                  void manifest.refetch()
                                },
                                // The server's refusals name what to do about them -- too few
                                // anchors, anchors collinear in the track, a residual too large
                                // to trust -- so they are shown rather than summarised.
                                onError: (error) =>
                                  setAlignError(
                                    error instanceof ApiError
                                      ? error.message
                                      : 'Could not place this capture on the plan',
                                  ),
                              },
                            )
                          }}
                        />
                      ) : null}

                      {/*
                        Sharing sits with the plan tools rather than in the page header: it is
                        about this frame at this date, which is exactly what the surrounding
                        panel is about, and a header button would invite sharing whatever
                        happened to be open.
                      */}
                      <ShareFrame frameId={frameId} />

                      {canPlace && orderedFrames.length >= 2 ? (
                        <RecognisePlacement
                          result={localizeResult}
                          cleared={clearedPlacement}
                          placed={orderedFrames.some((item) => item.location_id)}
                          pending={localize.isPending}
                          clearing={clearPlacement.isPending}
                          error={localizeError}
                          onRun={(options) => {
                            setLocalizeError(null)
                            setClearedPlacement(null)
                            localize.mutate(options, {
                              onSuccess: (result) => {
                                setLocalizeResult(result)
                                void manifest.refetch()
                                void sessionFrames.refetch()
                              },
                              // A refusal comes back as a 200 with `mode: none`, so anything that
                              // reaches here is a real failure rather than "nothing recognised".
                              onError: (error) =>
                                setLocalizeError(
                                  error instanceof ApiError
                                    ? error.message
                                    : 'Could not check this capture against earlier ones',
                                ),
                            })
                          }}
                          onClear={() => {
                            setLocalizeError(null)
                            setLocalizeResult(null)
                            clearPlacement.mutate(undefined, {
                              onSuccess: (result) => {
                                setClearedPlacement(result)
                                void manifest.refetch()
                                void sessionFrames.refetch()
                              },
                              onError: (error) =>
                                setLocalizeError(
                                  error instanceof ApiError
                                    ? error.message
                                    : 'Could not clear this placement',
                                ),
                            })
                          }}
                        />
                      ) : null}

                      {!frame?.location_id && canPlace ? (
                        <PlaceOnPlanForm
                          locations={locations.data ?? []}
                          pending={assign.isPending}
                          onAssign={(locationId) =>
                            assign.mutate(
                              { frameId, location_id: locationId },
                              {
                                onSuccess: () => {
                                  setNotice('Frame placed on the floor plan.')
                                  void manifest.refetch()
                                },
                                onError: (error) =>
                                  setNotice(
                                    error instanceof ApiError
                                      ? error.message
                                      : 'Could not place the frame',
                                  ),
                              },
                            )
                          }
                        />
                      ) : null}
                    </div>
                  ) : (
                    <Callout tone="info" title="No floor plan">
                      Upload a plan for this floor to place captures on it.
                    </Callout>
                  )
                ) : null}

                {panel === 'issues' ? (
                  <div className="space-y-2">
                    {annotations.data?.length ? (
                      <ul className="divide-y divide-line">
                        {annotations.data.map((annotation) => (
                          <li key={annotation.id} className="py-2">
                            <div className="flex items-start gap-2">
                              <Badge
                                tone={
                                  annotation.status === 'resolved'
                                    ? 'good'
                                    : annotation.priority === 'critical'
                                      ? 'bad'
                                      : 'warn'
                                }
                              >
                                {humanize(annotation.priority)}
                              </Badge>
                              <div className="min-w-0 flex-1">
                                <p className="text-sm font-medium text-ink">{annotation.title}</p>
                                {annotation.description ? (
                                  <p className="mt-0.5 text-xs text-ink-muted">
                                    {annotation.description}
                                  </p>
                                ) : null}
                                <p className="mt-0.5 text-2xs text-ink-subtle">
                                  {humanize(annotation.status)} ·{' '}
                                  {annotation.created_by_name ?? 'Unknown'} ·{' '}
                                  {formatDateTime(annotation.created_at)}
                                </p>
                              </div>
                            </div>
                          </li>
                        ))}
                      </ul>
                    ) : (
                      <EmptyState
                        title="No issues on this frame"
                        description={
                          canAnnotate
                            ? 'Use the annotation tool in the viewer, then click the panorama to pin a finding.'
                            : 'Site engineers and above can raise findings here.'
                        }
                      />
                    )}
                  </div>
                ) : null}
              </div>
            </Card>

            {session.data ? (
              <Card>
                <CardHeader title="Capture session" />
                <dl className="divide-y divide-line text-sm">
                  <Row label="Session" value={session.data.id} mono />
                  <Row label="Operator" value={session.data.operator_name ?? '—'} />
                  <Row label="Camera" value={session.data.camera_label ?? '—'} />
                  <Row label="Source" value={humanize(session.data.capture_source)} />
                  <Row label="Frames" value={String(session.data.frame_count)} />
                  <Row label="Started" value={formatDateTime(session.data.started_at)} />
                </dl>
                <div className="p-3">
                  <Button
                    size="sm"
                    variant="secondary"
                    className="w-full"
                    onClick={() => navigate(`/capture/${session.data.id}`)}
                  >
                    Open capture detail
                  </Button>
                </div>
              </Card>
            ) : null}

            {manifest.data?.neighbours.length ? (
              <Card>
                <CardHeader
                  title="Nearby positions"
                  description="Ordered by estimated distance from this frame"
                />
                <ul className="divide-y divide-line">
                  {manifest.data.neighbours.map((neighbour) => (
                    <li key={neighbour.frame_id}>
                      <button
                        type="button"
                        onClick={() => select(neighbour.frame_id)}
                        className="flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-surface-raised"
                      >
                        <MapPin className="size-3.5 shrink-0 text-brand" aria-hidden />
                        <span className="min-w-0 flex-1 truncate">
                          {neighbour.location_code ?? 'Unplaced frame'}
                        </span>
                        {neighbour.direction_deg !== null ? (
                          <span className="tnum text-2xs text-ink-subtle">
                            {Math.round(neighbour.direction_deg)}°
                          </span>
                        ) : null}
                      </button>
                    </li>
                  ))}
                </ul>
              </Card>
            ) : null}
          </div>
        </div>
      </PageBody>

      {pendingAnnotation && frameId ? (
        <AnnotationDialog
          frameId={frameId}
          yaw={pendingAnnotation.yaw}
          pitch={pendingAnnotation.pitch}
          onClose={() => setPendingAnnotation(null)}
          onCreated={() => {
            setPendingAnnotation(null)
            setPanel('issues')
            void annotations.refetch()
          }}
        />
      ) : null}
    </>
  )
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex items-center justify-between gap-2 px-3 py-1.5">
      <dt className="shrink-0 text-ink-muted">{label}</dt>
      <dd className={`truncate text-ink ${mono ? 'font-mono text-2xs' : ''}`}>{value}</dd>
    </div>
  )
}

function PlaceOnPlanForm({
  locations,
  onAssign,
  pending,
}: {
  locations: Array<{ id: string; code: string; label: string | null }>
  onAssign: (locationId: string) => void
  pending: boolean
}) {
  const [locationId, setLocationId] = useState('')

  return (
    <div className="space-y-2 rounded border border-line bg-surface-raised p-3">
      <p className="text-xs font-medium text-ink">Place this frame on the plan</p>
      {locations.length ? (
        <div className="flex items-end gap-2">
          <Field label="Capture point" className="flex-1">
            <Select value={locationId} onChange={(event) => setLocationId(event.target.value)}>
              <option value="">Select a point</option>
              {locations.map((location) => (
                <option key={location.id} value={location.id}>
                  {location.code}
                  {location.label ? ` — ${location.label}` : ''}
                </option>
              ))}
            </Select>
          </Field>
          <Button
            variant="primary"
            disabled={!locationId}
            loading={pending}
            onClick={() => locationId && onAssign(locationId)}
          >
            Place
          </Button>
        </div>
      ) : (
        <p className="text-xs text-ink-muted">
          No capture points on this floor yet. Add them under Floor Plans.
        </p>
      )}
    </div>
  )
}

function AnnotationDialog({
  frameId,
  yaw,
  pitch,
  onClose,
  onCreated,
}: {
  frameId: string
  yaw: number
  pitch: number
  onClose: () => void
  onCreated: () => void
}) {
  const mutation = useCreateAnnotation(frameId)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    const form = new FormData(event.currentTarget)
    try {
      await mutation.mutateAsync({
        title: String(form.get('title') ?? '').trim(),
        description: String(form.get('description') ?? '').trim() || null,
        priority: String(form.get('priority') ?? 'medium'),
        yaw_deg: yaw,
        pitch_deg: pitch,
      })
      onCreated()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not save the annotation')
    }
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title="Add annotation"
      description={`Pinned at ${yaw.toFixed(0)}° yaw, ${pitch.toFixed(0)}° pitch`}
    >
      <form onSubmit={onSubmit} className="space-y-3">
        <Field label="Title" required>
          <Input name="title" required placeholder="Missing ceiling panel above grid C4" autoFocus />
        </Field>
        <Field label="Description">
          <Textarea name="description" placeholder="What is wrong, and what needs to happen." />
        </Field>
        <Field label="Priority">
          <Select name="priority" defaultValue="medium">
            {ANNOTATION_PRIORITY_VALUES.map((priority) => (
              <option key={priority} value={priority}>
                {humanize(priority)}
              </option>
            ))}
          </Select>
        </Field>
        {error ? <Callout tone="bad">{error}</Callout> : null}
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={mutation.isPending}>
            Save annotation
          </Button>
        </div>
      </form>
    </Dialog>
  )
}
