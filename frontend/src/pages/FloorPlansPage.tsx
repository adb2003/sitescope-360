/** Floor plans: upload, calibrate, place capture points, draw the path (§12, §13). */

import { Map, Ruler, Upload } from 'lucide-react'
import { useEffect, useMemo, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'

import { PageBody, PageHeader } from '@/components/layout/AppShell'
import { FloorPlanCanvas, LocationList } from '@/components/floorplan/FloorPlanCanvas'
import { SheetPicker } from '@/components/floorplan/SheetPicker'
import { Metric, ProvenanceTag } from '@/components/status'
import {
  Badge,
  Button,
  Callout,
  Card,
  CardHeader,
  Dialog,
  EmptyState,
  Field,
  Input,
  Select,
  Skeleton,
} from '@/components/ui'
import {
  useCoverage,
  useCreateLocation,
  useDeleteLocation,
  useFloorPaths,
  useFloorPlan,
  useLocations,
  useMoveLocation,
  useProjectFloors,
  useUploadFloorPlan,
  useZones,
} from '@/hooks/queries'
import { Capability, useAuth } from '@/hooks/useAuth'
import { useProjectContext } from '@/hooks/useProjectContext'
import { api, ApiError } from '@/lib/api'
import { formatArea, formatPercent, humanize } from '@/lib/utils'
import type { CaptureLocation } from '@/types/api'

export function FloorPlansPage() {
  const { projectId } = useProjectContext()
  const { can } = useAuth()
  const navigate = useNavigate()

  const floors = useProjectFloors(projectId)
  const [floorId, setFloorId] = useState<string | null>(null)
  const [selectedLocationId, setSelectedLocationId] = useState<string | null>(null)
  const [uploading, setUploading] = useState(false)
  const [calibrating, setCalibrating] = useState(false)
  const [message, setMessage] = useState<string | null>(null)

  // Default to the first floor once the list arrives.
  useEffect(() => {
    if (!floorId && floors.data?.length) setFloorId(floors.data[0]?.id ?? null)
  }, [floors.data, floorId])

  const plan = useFloorPlan(floorId ?? undefined)
  const locations = useLocations(floorId ?? undefined)
  const paths = useFloorPaths(floorId ?? undefined)
  const zones = useZones(floorId ?? undefined)
  const coverage = useCoverage(projectId ?? undefined)

  const createLocation = useCreateLocation(floorId ?? '')
  const moveLocation = useMoveLocation(floorId ?? '')
  const deleteLocation = useDeleteLocation(floorId ?? '')

  const canEditPlan = can(Capability.FloorPlanEdit)
  const canEditPoints = can(Capability.LocationEdit)

  const floorCoverage = useMemo(
    () => coverage.data?.floors.find((item) => item.floor_id === floorId),
    [coverage.data, floorId],
  )
  const selected = locations.data?.find((item) => item.id === selectedLocationId) ?? null

  if (!projectId) {
    return (
      <PageBody>
        <Card>
          <EmptyState
            icon={Map}
            title="No project selected"
            description="Choose a project from the sidebar to work with its floor plans."
          />
        </Card>
      </PageBody>
    )
  }

  function openLocation(location: CaptureLocation) {
    setSelectedLocationId(location.id)
    if (location.latest_frame_id) {
      // Clicking a point on the plan opens the panorama taken there — the §12 requirement.
      navigate(`/viewer/${location.latest_frame_id}`)
    } else {
      setMessage(`${location.code} has no captures yet.`)
    }
  }

  return (
    <>
      <PageHeader
        title="Floor plans"
        subtitle="Place capture points, review the capture path, and open a panorama from the plan"
        actions={
          <>
            <Select
              value={floorId ?? ''}
              onChange={(event) => {
                setFloorId(event.target.value || null)
                setSelectedLocationId(null)
              }}
              className="w-48"
              aria-label="Floor"
            >
              <option value="">Select a floor</option>
              {floors.data?.map((floor) => (
                <option key={floor.id} value={floor.id}>
                  {floor.building_name} · {floor.name}
                </option>
              ))}
            </Select>
            {canEditPlan && floorId ? (
              <Button variant="secondary" icon={Upload} onClick={() => setUploading(true)}>
                Upload plan
              </Button>
            ) : null}
            {canEditPlan && plan.data ? (
              <Button variant="secondary" icon={Ruler} onClick={() => setCalibrating(true)}>
                Calibrate
              </Button>
            ) : null}
          </>
        }
      />

      <PageBody className="space-y-4">
        {message ? (
          <Callout tone="info" title={message}>
            Upload a capture for this floor and bind a frame to this point.
          </Callout>
        ) : null}

        {!floors.data?.length && !floors.isLoading ? (
          <Card>
            <EmptyState
              icon={Map}
              title="No floors yet"
              description="Add a building and at least one floor under Projects before uploading a plan."
              action={<Button onClick={() => navigate('/projects')}>Go to projects</Button>}
            />
          </Card>
        ) : null}

        {floorId ? (
          <div className="grid gap-3 xl:grid-cols-[1fr_320px]">
            <div className="space-y-3">
              {plan.isLoading ? (
                <Card className="p-4">
                  <Skeleton className="h-72" />
                </Card>
              ) : !plan.data ? (
                <Card>
                  <EmptyState
                    icon={Map}
                    title="This floor has no plan yet"
                    description="Upload a PNG or JPG export of the general-arrangement drawing. Capture points are placed in the raster's pixel coordinates, so a clean export works best."
                    action={
                      canEditPlan ? (
                        <Button variant="primary" icon={Upload} onClick={() => setUploading(true)}>
                          Upload floor plan
                        </Button>
                      ) : null
                    }
                  />
                </Card>
              ) : (
                <FloorPlanCanvas
                  plan={plan.data}
                  locations={locations.data ?? []}
                  paths={paths.data?.items ?? []}
                  selectedLocationId={selectedLocationId}
                  editable={canEditPoints}
                  onSelectLocation={openLocation}
                  onAddLocation={(x, y) => {
                    createLocation.mutate(
                      { x, y },
                      {
                        onError: (error) =>
                          setMessage(
                            error instanceof ApiError ? error.message : 'Could not add the point',
                          ),
                      },
                    )
                  }}
                  onMoveLocation={(id, x, y) => moveLocation.mutate({ id, x, y })}
                  onDeleteLocation={(id) => {
                    deleteLocation.mutate(id)
                    setSelectedLocationId(null)
                  }}
                  className="h-[560px]"
                />
              )}

              {paths.data?.items.some((path) => path.provenance === 'estimated') ? (
                <Callout tone="estimated" title="Some capture paths are estimated">
                  A dashed path was derived by visual positioning rather than manual placement. Its
                  shape is meaningful; its exact position on the plan is not guaranteed.
                </Callout>
              ) : null}
            </div>

            <div className="space-y-3">
              <Card>
                <CardHeader title="Floor coverage" description={coverage.data?.method} />
                <div className="space-y-3 p-4">
                  <Metric
                    label="Coverage"
                    value={formatPercent(floorCoverage?.coverage_ratio ?? null)}
                    provenance="manual"
                    progress={floorCoverage?.coverage_ratio ?? null}
                  />
                  <div className="grid grid-cols-2 gap-3">
                    <Metric
                      label="Expected points"
                      value={floorCoverage?.expected_capture_points ?? '—'}
                      provenance="manual"
                    />
                    <Metric
                      label="Visited"
                      value={floorCoverage?.visited_locations ?? '—'}
                      provenance="measured"
                    />
                    <Metric
                      label="Captured area"
                      value={formatArea(floorCoverage?.covered_area_sqft)}
                      provenance="manual"
                    />
                    <Metric
                      label="Pending area"
                      value={formatArea(floorCoverage?.pending_area_sqft)}
                      provenance="manual"
                    />
                  </div>
                </div>
              </Card>

              {plan.data ? (
                <Card>
                  <CardHeader title="Plan details" />
                  <dl className="divide-y divide-line text-sm">
                    <DetailRow label="Name" value={plan.data.name} />
                    <DetailRow
                      label="Raster"
                      value={`${plan.data.width_px} × ${plan.data.height_px} px`}
                    />
                    <DetailRow
                      label="Scale"
                      value={
                        plan.data.px_per_meter
                          ? `${plan.data.px_per_meter.toFixed(1)} px / m`
                          : 'Not calibrated'
                      }
                    />
                    <div className="flex items-center justify-between px-4 py-2">
                      <dt className="text-ink-muted">Calibration</dt>
                      <dd>
                        {plan.data.px_per_meter ? (
                          <ProvenanceTag
                            provenance={
                              (plan.data.calibration?.provenance as 'manual' | 'measured') ??
                              'manual'
                            }
                          />
                        ) : (
                          <Badge tone="neutral">None</Badge>
                        )}
                      </dd>
                    </div>
                  </dl>
                  {!plan.data.px_per_meter ? (
                    <div className="p-4 pt-0">
                      <Callout tone="info" title="Not calibrated">
                        Without a pixel-to-metre scale the plan still works for placement, but no
                        scale bar is drawn and no real-world distance is inferred.
                      </Callout>
                    </div>
                  ) : null}
                  {/*
                    Renders nothing unless this plan came from a PDF — an uploaded image has no
                    sheets to choose between, and the API says so rather than returning an empty
                    set.
                  */}
                  <SheetPicker planId={plan.data.id} />
                </Card>
              ) : null}

              <Card>
                <CardHeader
                  title="Capture points"
                  actions={<Badge tone="neutral">{locations.data?.length ?? 0}</Badge>}
                />
                <div className="max-h-72 overflow-y-auto">
                  <LocationList
                    locations={locations.data ?? []}
                    selectedLocationId={selectedLocationId}
                    onSelect={openLocation}
                    activePlanId={plan.data?.id ?? null}
                  />
                </div>
              </Card>

              {selected ? (
                <Card>
                  <CardHeader title={`${selected.code} details`} />
                  <dl className="divide-y divide-line text-sm">
                    <DetailRow label="Label" value={selected.label ?? '—'} />
                    <DetailRow
                      label="Plan position"
                      value={`${Math.round(selected.x)}, ${Math.round(selected.y)} px`}
                    />
                    <DetailRow label="Captures" value={String(selected.frame_count)} />
                    <DetailRow label="Provenance" value={humanize(selected.provenance)} />
                  </dl>
                </Card>
              ) : null}

              {zones.data?.length ? (
                <Card>
                  <CardHeader title="Zones" description="Expected capture points per zone" />
                  <ul className="divide-y divide-line text-sm">
                    {zones.data.map((zone) => (
                      <li key={zone.id} className="flex items-center gap-2 px-4 py-2">
                        <span className="min-w-0 flex-1 truncate text-ink">{zone.name}</span>
                        <span className="text-2xs text-ink-subtle">
                          {formatArea(zone.area_sqft)}
                        </span>
                        <Badge tone="neutral">{zone.expected_capture_points} pts</Badge>
                      </li>
                    ))}
                  </ul>
                </Card>
              ) : null}
            </div>
          </div>
        ) : null}
      </PageBody>

      {floorId ? (
        <UploadPlanDialog
          open={uploading}
          onClose={() => setUploading(false)}
          projectId={projectId}
          floorId={floorId}
        />
      ) : null}
      {plan.data && calibrating ? (
        <CalibrateDialog
          plan={plan.data}
          onClose={() => setCalibrating(false)}
          onDone={() => {
            setCalibrating(false)
            void plan.refetch()
          }}
        />
      ) : null}
    </>
  )
}

function DetailRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between gap-2 px-4 py-2">
      <dt className="shrink-0 text-ink-muted">{label}</dt>
      <dd className="tnum truncate text-ink">{value}</dd>
    </div>
  )
}

// ---- upload -----------------------------------------------------------------
function UploadPlanDialog({
  open,
  onClose,
  projectId,
  floorId,
}: {
  open: boolean
  onClose: () => void
  projectId: string
  floorId: string
}) {
  const mutation = useUploadFloorPlan(projectId, floorId)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    const elements = event.currentTarget.elements
    const input = elements.namedItem('file') as HTMLInputElement | null
    const file = input?.files?.[0]
    if (!file) {
      setError('Choose a file first')
      return
    }
    const pageInput = elements.namedItem('page') as HTMLInputElement | null
    const page = Number(pageInput?.value ?? 1)
    try {
      await mutation.mutateAsync({ file, page: Number.isFinite(page) ? page : 1 })
      onClose()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Upload failed')
    }
  }

  return (
    <Dialog open={open} onClose={onClose} title="Upload floor plan">
      <form onSubmit={onSubmit} className="space-y-3">
        <Field
          label="Drawing file"
          required
          hint="PNG, JPG or PDF. A PDF sheet is rasterised on upload, so capture points can be placed on it straight away."
        >
          <Input name="file" type="file" accept=".png,.jpg,.jpeg,.pdf" required />
        </Field>
        {/* Only one sheet becomes a floor plan. A set contains sections, elevations and details,
            and importing all of them as plans to place capture points on would be worse than
            importing none — so the operator names the sheet they want. */}
        <Field
          label="PDF sheet"
          hint="Which page of a multi-sheet PDF to use. Ignored for images."
        >
          <Input name="page" type="number" min={1} step={1} defaultValue={1} />
        </Field>
        {error ? <Callout tone="bad">{error}</Callout> : null}
        <Callout tone="info" title="Replacing an existing plan">
          The new plan becomes active. Earlier revisions are kept so existing capture points keep
          the plan they were placed against.
        </Callout>
        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={mutation.isPending}>
            Upload
          </Button>
        </div>
      </form>
    </Dialog>
  )
}

// ---- calibration ------------------------------------------------------------
function CalibrateDialog({
  plan,
  onClose,
  onDone,
}: {
  plan: { id: string; width_px: number; height_px: number }
  onClose: () => void
  onDone: () => void
}) {
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState(false)

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    setPending(true)
    const form = new FormData(event.currentTarget)
    try {
      await api.put(`/floorplans/${plan.id}/calibration`, {
        x1: Number(form.get('x1')),
        y1: Number(form.get('y1')),
        x2: Number(form.get('x2')),
        y2: Number(form.get('y2')),
        real_distance_m: Number(form.get('real_distance_m')),
      })
      onDone()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Calibration failed')
    } finally {
      setPending(false)
    }
  }

  return (
    <Dialog
      open
      onClose={onClose}
      title="Calibrate the plan"
      description="Enter two points on the drawing and the real distance between them."
    >
      <form onSubmit={onSubmit} className="space-y-3">
        <Callout tone="info" title="This is entered, not inferred">
          Take the two points from a dimension printed on the drawing. The platform stores the
          resulting scale as manually provided — nothing measures it from the image.
        </Callout>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Point 1 — X (px)" required>
            <Input name="x1" type="number" min={0} max={plan.width_px} required defaultValue={0} />
          </Field>
          <Field label="Point 1 — Y (px)" required>
            <Input name="y1" type="number" min={0} max={plan.height_px} required defaultValue={0} />
          </Field>
          <Field label="Point 2 — X (px)" required>
            <Input
              name="x2"
              type="number"
              min={0}
              max={plan.width_px}
              required
              defaultValue={plan.width_px}
            />
          </Field>
          <Field label="Point 2 — Y (px)" required>
            <Input name="y2" type="number" min={0} max={plan.height_px} required defaultValue={0} />
          </Field>
        </div>

        <Field label="Real distance between them (metres)" required>
          <Input name="real_distance_m" type="number" step="0.01" min={0.01} required />
        </Field>

        {error ? <Callout tone="bad">{error}</Callout> : null}

        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={pending}>
            Save calibration
          </Button>
        </div>
      </form>
    </Dialog>
  )
}
