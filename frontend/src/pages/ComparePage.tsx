/**
 * Date comparison (§17): two synchronised 360 viewers, or a slider wipe over the same location.
 *
 * The synchronisation is one-directional per drag: whichever viewer the user grabs becomes the
 * leader and pushes its orientation to the other. A bidirectional binding oscillates, because
 * each viewer's `setOrientation` would emit a change the other treats as user input.
 */

import { ArrowLeftRight, Columns2, Link2, Link2Off, SlidersHorizontal } from 'lucide-react'
import { useCallback, useMemo, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'

import { PageBody, PageHeader } from '@/components/layout/AppShell'
import { Viewer360 } from '@/components/viewer/Viewer360'
import type { Orientation } from '@/components/viewer/PanoramaCanvas'
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
  Tabs,
} from '@/components/ui'
import {
  useFrameComparison,
  useLocationComparison,
  useLocationFrames,
  useLocations,
  useProjectFloors,
} from '@/hooks/queries'
import { useProjectContext } from '@/hooks/useProjectContext'
import { formatDateTime } from '@/lib/utils'
import type { ViewerManifest } from '@/types/api'

type Mode = 'side-by-side' | 'slider'

export function ComparePage() {
  const { projectId } = useProjectContext()
  const [searchParams, setSearchParams] = useSearchParams()

  const floors = useProjectFloors(projectId)
  const [floorId, setFloorId] = useState<string | null>(null)
  const effectiveFloorId = floorId ?? floors.data?.[0]?.id ?? null
  const locations = useLocations(effectiveFloorId ?? undefined)

  const locationParam = searchParams.get('location')
  const locationId = locationParam ?? locations.data?.find((item) => item.frame_count > 1)?.id ?? null

  const [mode, setMode] = useState<Mode>('side-by-side')
  const [synced, setSynced] = useState(true)
  const [frameA, setFrameA] = useState<string | null>(null)
  const [frameB, setFrameB] = useState<string | null>(null)

  const history = useLocationFrames(locationId ?? undefined)
  // With no explicit pair chosen, ask the backend for the natural first/last comparison.
  const auto = useLocationComparison(frameA || frameB ? undefined : (locationId ?? undefined))
  const explicit = useFrameComparison(frameA ?? undefined, frameB ?? undefined)
  const comparison = frameA && frameB ? explicit : auto

  const [leader, setLeader] = useState<'a' | 'b' | null>(null)
  const [sharedOrientation, setSharedOrientation] = useState<Orientation | null>(null)
  const [wipe, setWipe] = useState(50)
  const wipeContainer = useRef<HTMLDivElement>(null)

  const onLeaderOrientation = useCallback(
    (side: 'a' | 'b') => (orientation: Orientation) => {
      if (!synced) return
      setLeader(side)
      setSharedOrientation(orientation)
    },
    [synced],
  )

  const manifestA = useMemo(
    () => (comparison.data ? toManifest(comparison.data.a) : null),
    [comparison.data],
  )
  const manifestB = useMemo(
    () => (comparison.data ? toManifest(comparison.data.b) : null),
    [comparison.data],
  )

  function selectLocation(id: string) {
    setFrameA(null)
    setFrameB(null)
    const next = new URLSearchParams(searchParams)
    next.set('location', id)
    setSearchParams(next, { replace: true })
  }

  if (!projectId) {
    return (
      <PageBody>
        <Card>
          <EmptyState
            icon={Columns2}
            title="No project selected"
            description="Choose a project from the sidebar to compare its capture dates."
          />
        </Card>
      </PageBody>
    )
  }

  const comparable = (locations.data ?? []).filter((item) => item.frame_count > 1)

  return (
    <>
      <PageHeader
        title="Compare dates"
        subtitle="Two visits to the same capture point, side by side or wiped"
        actions={
          <>
            <Select
              value={effectiveFloorId ?? ''}
              onChange={(event) => {
                setFloorId(event.target.value || null)
                setFrameA(null)
                setFrameB(null)
              }}
              className="w-44"
              aria-label="Floor"
            >
              {floors.data?.map((floor) => (
                <option key={floor.id} value={floor.id}>
                  {floor.building_name} · {floor.name}
                </option>
              ))}
            </Select>
            <Select
              value={locationId ?? ''}
              onChange={(event) => selectLocation(event.target.value)}
              className="w-52"
              aria-label="Capture point"
            >
              <option value="">Select a capture point</option>
              {comparable.map((location) => (
                <option key={location.id} value={location.id}>
                  {location.code} — {location.frame_count} visits
                </option>
              ))}
            </Select>
            <Tabs
              value={mode}
              onChange={setMode}
              options={[
                { value: 'side-by-side', label: 'Side by side' },
                { value: 'slider', label: 'Slider' },
              ]}
            />
            <Button
              variant={synced ? 'primary' : 'secondary'}
              size="sm"
              icon={synced ? Link2 : Link2Off}
              onClick={() => setSynced((value) => !value)}
              title="Synchronise the two viewers' orientation"
            >
              {synced ? 'Synced' : 'Independent'}
            </Button>
          </>
        }
      />

      <PageBody className="space-y-3">
        {!comparable.length && !locations.isLoading ? (
          <Card>
            <EmptyState
              icon={ArrowLeftRight}
              title="Nothing to compare yet"
              description="Comparison needs at least two visits bound to the same capture point. Capture the floor again, then place the new frames on the same points."
            />
          </Card>
        ) : null}

        {comparison.isError ? (
          <Card>
            <ErrorState
              title="Cannot compare these captures"
              message={comparison.error?.message}
              onRetry={() => void comparison.refetch()}
            />
          </Card>
        ) : null}

        {comparison.isLoading ? (
          <Card className="p-4">
            <Skeleton className="h-[60vh]" />
          </Card>
        ) : null}

        {comparison.data && manifestA && manifestB ? (
          <>
            {comparison.data.note ? (
              <Callout tone="warn" title="These frames may not show the same spot">
                {comparison.data.note}
              </Callout>
            ) : null}

            <div className="flex flex-wrap items-center gap-2">
              <Badge tone="brand">{comparison.data.a.location_code ?? 'Unplaced'}</Badge>
              <span className="text-xs text-ink-muted">
                {formatDateTime(comparison.data.a.captured_at)}
              </span>
              <ArrowLeftRight className="size-3.5 text-ink-subtle" aria-hidden />
              <span className="text-xs text-ink-muted">
                {formatDateTime(comparison.data.b.captured_at)}
              </span>
              <Badge tone="info">{comparison.data.days_apart} days apart</Badge>
            </div>

            {mode === 'side-by-side' ? (
              <div className="grid gap-3 lg:grid-cols-2">
                <Viewer360
                  manifest={manifestA}
                  compact
                  onOrientationChange={onLeaderOrientation('a')}
                  followOrientation={synced && leader === 'b' ? sharedOrientation : null}
                  className="h-[52vh] min-h-[320px]"
                />
                <Viewer360
                  manifest={manifestB}
                  compact
                  onOrientationChange={onLeaderOrientation('b')}
                  followOrientation={synced && leader === 'a' ? sharedOrientation : null}
                  className="h-[52vh] min-h-[320px]"
                />
              </div>
            ) : (
              <div
                ref={wipeContainer}
                className="relative h-[58vh] min-h-[360px] overflow-hidden panel"
              >
                <Viewer360
                  manifest={manifestA}
                  compact
                  onOrientationChange={onLeaderOrientation('a')}
                  followOrientation={synced && leader === 'b' ? sharedOrientation : null}
                  className="absolute inset-0 h-full"
                />
                {/* The "after" image is clipped to the wipe position. Both viewers stay live, so
                    dragging either side still rotates both. */}
                <div
                  className="absolute inset-0"
                  style={{ clipPath: `inset(0 0 0 ${wipe}%)` }}
                >
                  <Viewer360
                    manifest={manifestB}
                    compact
                    onOrientationChange={onLeaderOrientation('b')}
                    followOrientation={synced && leader === 'a' ? sharedOrientation : null}
                    className="absolute inset-0 h-full"
                  />
                </div>

                <div
                  className="pointer-events-none absolute inset-y-0 w-0.5 bg-brand"
                  style={{ left: `${wipe}%` }}
                />

                <div className="absolute bottom-3 left-1/2 z-10 flex -translate-x-1/2 items-center gap-2 rounded bg-black/70 px-3 py-1.5 backdrop-blur">
                  <SlidersHorizontal className="size-3.5 text-white/70" aria-hidden />
                  <input
                    type="range"
                    min={0}
                    max={100}
                    value={wipe}
                    onChange={(event) => setWipe(Number(event.target.value))}
                    className="w-48 accent-brand"
                    aria-label="Comparison wipe position"
                  />
                  <span className="tnum w-8 text-2xs text-white/80">{wipe}%</span>
                </div>
              </div>
            )}

            <div className="grid gap-3 lg:grid-cols-2">
              <FrameFacts label="Earlier" frame={comparison.data.a} />
              <FrameFacts label="Later" frame={comparison.data.b} />
            </div>

            {history.data && history.data.items.length > 2 ? (
              <Card>
                <CardHeader
                  title="Pick a different pair"
                  description={`${history.data.total} visits recorded at this point`}
                />
                <div className="flex gap-2 overflow-x-auto p-3">
                  {history.data.items.map((item) => {
                    const isA = item.id === comparison.data?.a.id
                    const isB = item.id === comparison.data?.b.id
                    return (
                      <div key={item.id} className="shrink-0" style={{ width: 120 }}>
                        {item.thumbnail?.url ? (
                          <img
                            src={item.thumbnail.url}
                            alt=""
                            loading="lazy"
                            className="h-14 w-full rounded border border-line object-cover"
                          />
                        ) : (
                          <div className="h-14 w-full rounded border border-line bg-surface-raised" />
                        )}
                        <p className="mt-1 truncate text-2xs text-ink-muted">
                          {formatDateTime(item.captured_at)}
                        </p>
                        <div className="mt-1 flex gap-1">
                          <Button
                            size="sm"
                            variant={isA ? 'primary' : 'secondary'}
                            className="h-6 flex-1 px-1 text-2xs"
                            onClick={() => setFrameA(item.id)}
                          >
                            A
                          </Button>
                          <Button
                            size="sm"
                            variant={isB ? 'primary' : 'secondary'}
                            className="h-6 flex-1 px-1 text-2xs"
                            onClick={() => setFrameB(item.id)}
                          >
                            B
                          </Button>
                        </div>
                      </div>
                    )
                  })}
                </div>
              </Card>
            ) : null}

            <Callout tone="info" title="What this comparison does and does not do">
              It shows the same viewpoint on two dates so a person can see what changed. The
              platform does not compute a progress percentage from the difference — that needs a
              trained progress model, which is a Phase 3 capability.
            </Callout>
          </>
        ) : null}
      </PageBody>
    </>
  )
}

function FrameFacts({
  label,
  frame,
}: {
  label: string
  frame: { captured_at: string; quality: { quality_score: number } | null; width_px: number; height_px: number; session_id: string }
}) {
  return (
    <Card className="p-3">
      <p className="label">{label}</p>
      <dl className="mt-1.5 space-y-1 text-xs">
        <div className="flex justify-between gap-2">
          <dt className="text-ink-muted">Captured</dt>
          <dd className="text-ink">{formatDateTime(frame.captured_at)}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-ink-muted">Resolution</dt>
          <dd className="tnum text-ink">
            {frame.width_px} × {frame.height_px}
          </dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-ink-muted">Quality score</dt>
          <dd className="tnum text-ink">
            {frame.quality ? `${Math.round(frame.quality.quality_score * 100)}%` : '—'}
          </dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-ink-muted">Session</dt>
          <dd className="truncate font-mono text-2xs text-ink">{frame.session_id}</dd>
        </div>
      </dl>
    </Card>
  )
}

/**
 * The compare endpoint returns frame details, not full viewer manifests. Wrapping them keeps
 * `Viewer360` to a single input shape rather than giving it two code paths.
 */
function toManifest(frame: ViewerManifest['frame']): ViewerManifest {
  return { frame, neighbours: [], previous_frame_id: null, next_frame_id: null, history: [] }
}
