/** Project list, creation, and the building/floor/zone builder (§22). */

import { Building2, ChevronRight, Layers, Plus, Search } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'

import { PageBody, PageHeader } from '@/components/layout/AppShell'
import {
  Badge,
  Button,
  Callout,
  Card,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  Input,
  Select,
  Skeleton,
  Table,
  Td,
  Th,
  Tr,
} from '@/components/ui'
import {
  useBuildings,
  useCreateBuilding,
  useCreateFloor,
  useCreateProject,
  useCreateZone,
  useProjectFloors,
  useProjects,
} from '@/hooks/queries'
import { Capability, useAuth } from '@/hooks/useAuth'
import { useProjectContext } from '@/hooks/useProjectContext'
import { ApiError } from '@/lib/api'
import { formatArea, formatNumber, formatRelative, humanize } from '@/lib/utils'
import { PROJECT_STATUS_VALUES } from '../../../shared/contracts'

export function ProjectsPage() {
  const [search, setSearch] = useState('')
  const { data, isLoading, isError, error, refetch } = useProjects(
    search.trim() ? { q: search.trim() } : {},
  )
  const { can } = useAuth()
  const { projectId, setProjectId } = useProjectContext()
  const navigate = useNavigate()
  const [creating, setCreating] = useState(false)
  const [buildingFor, setBuildingFor] = useState<string | null>(null)

  const canWrite = can(Capability.ProjectWrite)

  return (
    <>
      <PageHeader
        title="Projects"
        subtitle="Project → Building → Floor → Zone"
        actions={
          canWrite ? (
            <Button variant="primary" icon={Plus} onClick={() => setCreating(true)}>
              Create project
            </Button>
          ) : null
        }
      />

      <PageBody className="space-y-4">
        <div className="flex items-center gap-2">
          <div className="relative max-w-xs flex-1">
            <Search
              className="pointer-events-none absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-ink-subtle"
              aria-hidden
            />
            <Input
              value={search}
              onChange={(event) => setSearch(event.target.value)}
              placeholder="Search name, code or client"
              className="pl-7"
              aria-label="Search projects"
            />
          </div>
        </div>

        {isError ? (
          <Card>
            <ErrorState message={error?.message} onRetry={() => void refetch()} />
          </Card>
        ) : null}

        <Card>
          {isLoading ? (
            <div className="space-y-2 p-4">
              {[0, 1, 2].map((index) => (
                <Skeleton key={index} className="h-10" />
              ))}
            </div>
          ) : !data?.items.length ? (
            <EmptyState
              icon={Building2}
              title={search ? 'No projects match that search' : 'No projects yet'}
              description={
                search
                  ? 'Try a different name, code or client.'
                  : 'A project holds buildings, floors and zones. Create one to begin capturing.'
              }
              action={
                canWrite && !search ? (
                  <Button variant="primary" icon={Plus} onClick={() => setCreating(true)}>
                    Create project
                  </Button>
                ) : null
              }
            />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Project</Th>
                  <Th>Client</Th>
                  <Th>Status</Th>
                  <Th className="text-right">Buildings</Th>
                  <Th className="text-right">Floors</Th>
                  <Th className="text-right">Zones</Th>
                  <Th className="text-right">Points</Th>
                  <Th className="text-right">Captures</Th>
                  <Th>Last capture</Th>
                  <Th className="w-24" />
                </tr>
              </thead>
              <tbody>
                {data.items.map((project) => (
                  <Tr key={project.id}>
                    <Td>
                      <button
                        type="button"
                        className="text-left"
                        onClick={() => {
                          setProjectId(project.id)
                          navigate(`/projects/${project.id}`)
                        }}
                      >
                        <span className="block font-medium text-ink hover:underline">
                          {project.name}
                        </span>
                        <span className="block font-mono text-2xs text-ink-subtle">
                          {project.code}
                        </span>
                      </button>
                    </Td>
                    <Td className="text-xs text-ink-muted">{project.client ?? '—'}</Td>
                    <Td>
                      <Badge tone={project.status === 'active' ? 'good' : 'neutral'}>
                        {humanize(project.status)}
                      </Badge>
                    </Td>
                    <Td className="tnum text-right">{project.building_count}</Td>
                    <Td className="tnum text-right">{project.floor_count}</Td>
                    <Td className="tnum text-right">{project.zone_count}</Td>
                    <Td className="tnum text-right">{project.location_count}</Td>
                    <Td className="tnum text-right">{project.capture_session_count}</Td>
                    <Td className="text-xs text-ink-muted">
                      {formatRelative(project.last_capture_at)}
                    </Td>
                    <Td>
                      <div className="flex items-center justify-end gap-1">
                        {canWrite ? (
                          <Button
                            size="sm"
                            variant="ghost"
                            icon={Layers}
                            onClick={() => setBuildingFor(project.id)}
                            title="Add buildings, floors and zones"
                          >
                            Structure
                          </Button>
                        ) : null}
                        <ChevronRight className="size-3.5 text-ink-subtle" aria-hidden />
                      </div>
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>

        {projectId ? null : (
          <Callout tone="info" title="Select a project">
            Pick a project from the sidebar (or click one above) to unlock Floor Plans, the 360
            Viewer, Timeline, Compare and Reports.
          </Callout>
        )}
      </PageBody>

      <CreateProjectDialog open={creating} onClose={() => setCreating(false)} />
      {buildingFor ? (
        <StructureDialog projectId={buildingFor} onClose={() => setBuildingFor(null)} />
      ) : null}
    </>
  )
}

// ---- create project ---------------------------------------------------------
function CreateProjectDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const mutation = useCreateProject()
  const navigate = useNavigate()
  const { setProjectId } = useProjectContext()
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [formError, setFormError] = useState<string | null>(null)

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setFieldErrors({})
    setFormError(null)
    const form = new FormData(event.currentTarget)

    const payload: Record<string, unknown> = {
      name: String(form.get('name') ?? '').trim(),
      code: String(form.get('code') ?? '').trim(),
      client: emptyToNull(form.get('client')),
      location: emptyToNull(form.get('location')),
      status: String(form.get('status') ?? 'active'),
      start_date: emptyToNull(form.get('start_date')),
      expected_completion_date: emptyToNull(form.get('expected_completion_date')),
      expected_captures_per_week: Number(form.get('expected_captures_per_week') ?? 2),
    }

    try {
      const project = await mutation.mutateAsync(payload)
      setProjectId(project.id)
      onClose()
      navigate(`/projects/${project.id}`)
    } catch (caught) {
      if (caught instanceof ApiError) {
        setFieldErrors(caught.fieldErrors)
        // A 409 (duplicate code) has no field path, so it needs a form-level message.
        if (!Object.keys(caught.fieldErrors).length) setFormError(caught.message)
      } else {
        setFormError('Could not reach the server')
      }
    }
  }

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="Create project"
      description="A project is the top of the hierarchy: buildings, floors and zones sit under it."
      wide
    >
      <form id="create-project" onSubmit={onSubmit} className="grid gap-3 sm:grid-cols-2">
        <Field label="Project name" required error={fieldErrors.name}>
          <Input name="name" required placeholder="Chevron Phase 2" invalid={Boolean(fieldErrors.name)} />
        </Field>
        <Field
          label="Project code"
          required
          error={fieldErrors.code}
          hint="Short, unique. Used in reports and file paths."
        >
          <Input name="code" required placeholder="CVX-P2" invalid={Boolean(fieldErrors.code)} />
        </Field>
        <Field label="Client" error={fieldErrors.client}>
          <Input name="client" placeholder="Chevron" />
        </Field>
        <Field label="Location" error={fieldErrors.location}>
          <Input name="location" placeholder="Bakersfield, CA" />
        </Field>
        <Field label="Status">
          <Select name="status" defaultValue="active">
            {PROJECT_STATUS_VALUES.map((status) => (
              <option key={status} value={status}>
                {humanize(status)}
              </option>
            ))}
          </Select>
        </Field>
        <Field
          label="Expected captures per week"
          hint="Drives the due / overdue calculation."
          error={fieldErrors.expected_captures_per_week}
        >
          <Input name="expected_captures_per_week" type="number" min={0} max={50} defaultValue={2} />
        </Field>
        <Field label="Start date" error={fieldErrors.start_date}>
          <Input name="start_date" type="date" />
        </Field>
        <Field label="Expected completion" error={fieldErrors.expected_completion_date}>
          <Input name="expected_completion_date" type="date" />
        </Field>

        {formError ? (
          <div className="sm:col-span-2">
            <Callout tone="bad">{formError}</Callout>
          </div>
        ) : null}

        <div className="flex justify-end gap-2 sm:col-span-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={mutation.isPending}>
            Create project
          </Button>
        </div>
      </form>
    </Dialog>
  )
}

// ---- structure builder ------------------------------------------------------
function StructureDialog({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const buildings = useBuildings(projectId)
  const floors = useProjectFloors(projectId)
  const createBuilding = useCreateBuilding(projectId)
  const createFloor = useCreateFloor(projectId)
  const [zoneFloorId, setZoneFloorId] = useState<string | null>(null)

  return (
    <Dialog
      open
      onClose={onClose}
      title="Project structure"
      description="Buildings hold floors; floors hold zones and a floor plan."
      wide
    >
      <div className="space-y-4">
        <section className="space-y-2">
          <h3 className="text-sm font-semibold text-ink">Buildings</h3>
          {buildings.data?.length ? (
            <ul className="flex flex-wrap gap-1.5">
              {buildings.data.map((building) => (
                <li key={building.id}>
                  <Badge tone="brand">{building.name}</Badge>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-xs text-ink-muted">No buildings yet.</p>
          )}
          <form
            className="flex items-end gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              const form = new FormData(event.currentTarget)
              const name = String(form.get('name') ?? '').trim()
              if (!name) return
              createBuilding.mutate({ name })
              event.currentTarget.reset()
            }}
          >
            <Field label="Add building" className="flex-1">
              <Input name="name" placeholder="Main Office" />
            </Field>
            <Button type="submit" icon={Plus} loading={createBuilding.isPending}>
              Add
            </Button>
          </form>
        </section>

        <section className="space-y-2 border-t border-line pt-4">
          <h3 className="text-sm font-semibold text-ink">Floors</h3>
          {floors.data?.length ? (
            <Table>
              <thead>
                <tr>
                  <Th>Floor</Th>
                  <Th>Building</Th>
                  <Th className="text-right">Level</Th>
                  <Th className="text-right">Area</Th>
                  <Th className="text-right">Zones</Th>
                  <Th className="text-right">Points</Th>
                  <Th className="w-20" />
                </tr>
              </thead>
              <tbody>
                {floors.data.map((floor) => (
                  <Tr key={floor.id}>
                    <Td className="font-medium">{floor.name}</Td>
                    <Td className="text-xs text-ink-muted">{floor.building_name}</Td>
                    <Td className="tnum text-right">{floor.level}</Td>
                    <Td className="tnum text-right text-xs">{formatArea(floor.area_sqft)}</Td>
                    <Td className="tnum text-right">{floor.zone_count}</Td>
                    <Td className="tnum text-right">{floor.location_count}</Td>
                    <Td>
                      <Button size="sm" variant="ghost" onClick={() => setZoneFloorId(floor.id)}>
                        Zones
                      </Button>
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          ) : (
            <p className="text-xs text-ink-muted">No floors yet. Add a building first.</p>
          )}

          {buildings.data?.length ? (
            <form
              className="grid items-end gap-2 sm:grid-cols-[1fr_1fr_80px_100px_auto]"
              onSubmit={(event) => {
                event.preventDefault()
                const form = new FormData(event.currentTarget)
                const buildingId = String(form.get('building_id') ?? '')
                const name = String(form.get('name') ?? '').trim()
                if (!buildingId || !name) return
                const area = form.get('area_sqft')
                createFloor.mutate({
                  buildingId,
                  name,
                  level: Number(form.get('level') ?? 0),
                  ...(area ? { area_sqft: Number(area) } : {}),
                })
                event.currentTarget.reset()
              }}
            >
              <Field label="Building">
                <Select name="building_id">
                  {buildings.data.map((building) => (
                    <option key={building.id} value={building.id}>
                      {building.name}
                    </option>
                  ))}
                </Select>
              </Field>
              <Field label="Floor name">
                <Input name="name" placeholder="Level 03" />
              </Field>
              <Field label="Level">
                <Input name="level" type="number" defaultValue={0} />
              </Field>
              <Field label="Area (sq ft)">
                <Input name="area_sqft" type="number" min={0} placeholder="10000" />
              </Field>
              <Button type="submit" icon={Plus} loading={createFloor.isPending}>
                Add
              </Button>
            </form>
          ) : null}
        </section>

        {zoneFloorId ? (
          <ZoneSection
            projectId={projectId}
            floorId={zoneFloorId}
            onDone={() => setZoneFloorId(null)}
          />
        ) : null}

        <Callout tone="info" title="Why zones matter">
          Coverage is the ratio of visited capture points to the points you declare here per zone.
          Without zones, coverage falls back to counting the capture points that exist on the plan.
        </Callout>
      </div>
    </Dialog>
  )
}

function ZoneSection({
  projectId,
  floorId,
  onDone,
}: {
  projectId: string
  floorId: string
  onDone: () => void
}) {
  const createZone = useCreateZone(projectId, floorId)
  const floors = useProjectFloors(projectId)
  const floor = floors.data?.find((item) => item.id === floorId)

  return (
    <section className="space-y-2 border-t border-line pt-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-ink">
          Zones on {floor?.name ?? 'floor'}
          <span className="ml-2 text-xs font-normal text-ink-muted">
            {formatNumber(floor?.zone_count)} defined
          </span>
        </h3>
        <Button size="sm" variant="ghost" onClick={onDone}>
          Done
        </Button>
      </div>
      <form
        className="grid items-end gap-2 sm:grid-cols-[1fr_110px_130px_auto]"
        onSubmit={(event) => {
          event.preventDefault()
          const form = new FormData(event.currentTarget)
          const name = String(form.get('name') ?? '').trim()
          if (!name) return
          const area = form.get('area_sqft')
          createZone.mutate({
            name,
            ...(area ? { area_sqft: Number(area) } : {}),
            expected_capture_points: Number(form.get('expected_capture_points') ?? 1),
          })
          event.currentTarget.reset()
        }}
      >
        <Field label="Zone name">
          <Input name="name" placeholder="North Wing" />
        </Field>
        <Field label="Area (sq ft)">
          <Input name="area_sqft" type="number" min={0} placeholder="4000" />
        </Field>
        <Field label="Expected points" hint="Per visit">
          <Input name="expected_capture_points" type="number" min={0} defaultValue={4} />
        </Field>
        <Button type="submit" icon={Plus} loading={createZone.isPending}>
          Add zone
        </Button>
      </form>
    </section>
  )
}

function emptyToNull(value: FormDataEntryValue | null): string | null {
  const text = String(value ?? '').trim()
  return text ? text : null
}