/**
 * Settings: capture schedule, cameras, processing jobs, users, and the capability report.
 *
 * The Capabilities tab is the §46 contract made visible: it reads
 * `GET /api/system/capabilities` and renders exactly what the backend says is implemented,
 * estimated, or unavailable. Nothing on this page is hard-coded, so a capability cannot appear
 * to work in the UI after being removed from the server.
 */

import {
  Camera,
  CircleSlash,
  Cpu,
  ListChecks,
  Save,
  ShieldCheck,
  Users,
} from 'lucide-react'
import { useState, type FormEvent } from 'react'

import { PageBody, PageHeader } from '@/components/layout/AppShell'
import { JobStatusBadge, Metric, ProvenanceTag } from '@/components/status'
import {
  Badge,
  Button,
  Callout,
  Card,
  CardHeader,
  EmptyState,
  Field,
  Input,
  Select,
  Skeleton,
  Table,
  Tabs,
  Td,
  Th,
  Tr,
} from '@/components/ui'
import {
  useCameraProviders,
  useCameras,
  useCapabilities,
  useJobSummary,
  useJobs,
  useProjectFloors,
  useSchedules,
  useUpsertSchedule,
  useUsers,
} from '@/hooks/queries'
import { Capability, useAuth } from '@/hooks/useAuth'
import { useProjectContext } from '@/hooks/useProjectContext'
import { ApiError } from '@/lib/api'
import { formatBytes, formatDateTime, formatDuration, humanize } from '@/lib/utils'

type Tab = 'schedule' | 'cameras' | 'jobs' | 'capabilities' | 'users'

export function SettingsPage() {
  const { can } = useAuth()
  const [tab, setTab] = useState<Tab>('schedule')

  const tabs: Array<{ value: Tab; label: string }> = [
    { value: 'schedule', label: 'Capture schedule' },
    { value: 'cameras', label: 'Cameras' },
    { value: 'capabilities', label: 'Capabilities' },
    ...(can(Capability.JobAdmin) ? ([{ value: 'jobs' as Tab, label: 'Processing' }]) : []),
    ...(can(Capability.UserManage) ? ([{ value: 'users' as Tab, label: 'Users' }]) : []),
  ]

  return (
    <>
      <PageHeader
        title="Settings"
        subtitle="Capture cadence, camera devices, processing, and what this platform can honestly do"
      />

      <PageBody className="space-y-4">
        <Tabs value={tab} onChange={setTab} options={tabs} className="w-fit" />

        {tab === 'schedule' ? <ScheduleTab /> : null}
        {tab === 'cameras' ? <CamerasTab /> : null}
        {tab === 'capabilities' ? <CapabilitiesTab /> : null}
        {tab === 'jobs' ? <JobsTab /> : null}
        {tab === 'users' ? <UsersTab /> : null}
      </PageBody>
    </>
  )
}

// ---- schedule ---------------------------------------------------------------
function ScheduleTab() {
  const { projectId } = useProjectContext()
  const { can } = useAuth()
  const schedules = useSchedules(projectId)
  const floors = useProjectFloors(projectId)
  const upsert = useUpsertSchedule(projectId ?? '')
  const [message, setMessage] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const canEdit = can(Capability.SettingsProject)

  if (!projectId) {
    return (
      <Card>
        <EmptyState
          title="No project selected"
          description="Choose a project from the sidebar to configure its capture cadence."
        />
      </Card>
    )
  }

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setMessage(null)
    setError(null)
    const form = new FormData(event.currentTarget)
    try {
      await upsert.mutateAsync({
        floor_id: String(form.get('floor_id') ?? '') || null,
        expected_per_week: Number(form.get('expected_per_week') ?? 2),
        grace_days: Number(form.get('grace_days') ?? 1),
        notes: String(form.get('notes') ?? '').trim() || undefined,
      })
      setMessage('Schedule saved.')
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not save the schedule')
    }
  }

  return (
    <div className="grid gap-3 lg:grid-cols-2">
      <Card>
        <CardHeader
          title="Capture cadence"
          description="Drives the due / overdue calculation on the dashboards (§20)"
        />
        <form onSubmit={onSubmit} className="space-y-3 p-4">
          <Field label="Applies to">
            <Select name="floor_id" defaultValue="">
              <option value="">Whole project (default)</option>
              {floors.data?.map((floor) => (
                <option key={floor.id} value={floor.id}>
                  {floor.building_name} · {floor.name}
                </option>
              ))}
            </Select>
          </Field>
          <Field
            label="Expected captures per week"
            required
            hint="Two per week means one expected every 3.5 days."
          >
            <Input name="expected_per_week" type="number" min={0} max={50} defaultValue={2} required />
          </Field>
          <Field
            label="Grace days"
            hint="How long past due before a project is reported overdue."
          >
            <Input name="grace_days" type="number" min={0} max={30} defaultValue={1} />
          </Field>
          <Field label="Notes">
            <Input name="notes" placeholder="Two walkthroughs per week while fit-out is running" />
          </Field>

          {message ? <Callout tone="info">{message}</Callout> : null}
          {error ? <Callout tone="bad">{error}</Callout> : null}

          <Button
            type="submit"
            variant="primary"
            icon={Save}
            loading={upsert.isPending}
            disabled={!canEdit}
          >
            Save schedule
          </Button>
          {!canEdit ? (
            <p className="text-2xs text-ink-subtle">
              Requires the Project Manager role or higher.
            </p>
          ) : null}
        </form>
      </Card>

      <Card>
        <CardHeader title="Configured schedules" />
        {schedules.isLoading ? (
          <div className="p-4">
            <Skeleton className="h-8" />
          </div>
        ) : !schedules.data?.length ? (
          <EmptyState
            title="Using the project default"
            description="No explicit schedule configured; the project's captures-per-week value applies."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Scope</Th>
                <Th className="text-right">Per week</Th>
                <Th className="text-right">Grace</Th>
                <Th>Notes</Th>
              </tr>
            </thead>
            <tbody>
              {schedules.data.map((schedule) => (
                <Tr key={schedule.id}>
                  <Td>
                    {schedule.floor_id
                      ? (floors.data?.find((floor) => floor.id === schedule.floor_id)?.name ??
                        'Floor')
                      : 'Whole project'}
                  </Td>
                  <Td className="tnum text-right">{schedule.expected_per_week}</Td>
                  <Td className="tnum text-right">{schedule.grace_days}d</Td>
                  <Td className="max-w-xs truncate text-xs text-ink-muted">
                    {schedule.notes ?? '—'}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  )
}

// ---- cameras ----------------------------------------------------------------
function CamerasTab() {
  const providers = useCameraProviders()
  const devices = useCameras()

  return (
    <div className="space-y-3">
      <Callout tone="info" title="No camera integration is required">
        The default workflow is: record on any 360 camera, copy the files, upload them. Everything
        below is optional.
      </Callout>

      <Card>
        <CardHeader
          title="Camera providers"
          description="What the platform can talk to — and what it cannot"
        />
        {providers.isLoading ? (
          <div className="p-4">
            <Skeleton className="h-20" />
          </div>
        ) : (
          <ul className="divide-y divide-line">
            {providers.data?.map((provider) => (
              <li key={provider.key} className="p-4">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="font-medium text-ink">{provider.display_name}</span>
                  <Badge tone={provider.implemented ? 'good' : 'neutral'}>
                    {provider.implemented ? 'Implemented' : 'Not implemented'}
                  </Badge>
                  <Badge tone="neutral">{humanize(provider.transport)}</Badge>
                  {provider.supports_live_transfer ? (
                    <Badge tone="info">Live transfer</Badge>
                  ) : null}
                  {provider.supports_remote_trigger ? (
                    <Badge tone="info">Remote trigger</Badge>
                  ) : null}
                </div>
                <p className="mt-1 text-xs leading-relaxed text-ink-muted">
                  {provider.description}
                </p>
                {provider.setup_hint ? (
                  <p className="mt-1 text-2xs text-ink-subtle">{provider.setup_hint}</p>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card>
        <CardHeader title="Registered devices" />
        {devices.isLoading ? (
          <div className="p-4">
            <Skeleton className="h-10" />
          </div>
        ) : !devices.data?.length ? (
          <EmptyState
            icon={Camera}
            title="No devices registered"
            description="Registering a device is optional — it labels captures and enables folder import."
          />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Label</Th>
                <Th>Make / model</Th>
                <Th>Provider</Th>
                <Th>Source</Th>
                <Th>Last seen</Th>
                <Th>Active</Th>
              </tr>
            </thead>
            <tbody>
              {devices.data.map((device) => (
                <Tr key={device.id}>
                  <Td className="font-medium">{device.label}</Td>
                  <Td className="text-xs text-ink-muted">
                    {[device.make, device.model].filter(Boolean).join(' ') || '—'}
                  </Td>
                  <Td className="text-xs">{device.provider_key}</Td>
                  <Td className="text-xs text-ink-muted">{humanize(device.capture_source)}</Td>
                  <Td className="text-xs text-ink-muted">{formatDateTime(device.last_seen_at)}</Td>
                  <Td>
                    <Badge tone={device.is_active ? 'good' : 'neutral'}>
                      {device.is_active ? 'Active' : 'Inactive'}
                    </Badge>
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>
    </div>
  )
}

// ---- capabilities (§46) -----------------------------------------------------
function CapabilitiesTab() {
  const capabilities = useCapabilities()

  if (capabilities.isLoading) {
    return (
      <Card className="p-4">
        <Skeleton className="h-40" />
      </Card>
    )
  }
  if (!capabilities.data) return null

  const { vision, bim, independence, processing, uploads, mapping_confidence_threshold } =
    capabilities.data

  return (
    <div className="space-y-3">
      <Card>
        <CardHeader
          title="Independence"
          description="Read directly from the running server, not from this page"
          actions={<ShieldCheck className="size-4 text-good" aria-hidden />}
        />
        <div className="p-4">
          <p className="text-sm leading-relaxed text-ink">{independence.statement}</p>
          <p className="mt-2 text-2xs text-ink-subtle">
            Third-party monitoring platforms in use:{' '}
            {independence.third_party_monitoring_platforms.length
              ? independence.third_party_monitoring_platforms.join(', ')
              : 'none'}
          </p>
        </div>
      </Card>

      <Card>
        <CardHeader
          title="Vision and analysis"
          description="Each capability with its method and the provenance of what it produces"
        />
        <ul className="divide-y divide-line">
          {Object.entries(vision).map(([name, entry]) => (
            <li key={name} className="p-4">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium text-ink">{humanize(name)}</span>
                {entry.implemented ? (
                  <Badge tone="good" icon={ListChecks}>
                    Implemented
                  </Badge>
                ) : (
                  <Badge tone="neutral" icon={CircleSlash}>
                    Not implemented{entry.phase ? ` · Phase ${entry.phase}` : ''}
                  </Badge>
                )}
                {entry.provenance ? <ProvenanceTag provenance={entry.provenance} /> : null}
              </div>
              {entry.method ? (
                <p className="mt-1 font-mono text-2xs text-ink-muted">{entry.method}</p>
              ) : null}
              {entry.note ? <p className="mt-1 text-xs text-ink-muted">{entry.note}</p> : null}
              {entry.limitations?.length ? (
                <ul className="mt-1.5 list-inside list-disc space-y-0.5 text-xs text-warn">
                  {entry.limitations.map((limitation) => (
                    <li key={limitation}>{limitation}</li>
                  ))}
                </ul>
              ) : null}
            </li>
          ))}
        </ul>
      </Card>

      <div className="grid gap-3 lg:grid-cols-2">
        <Card>
          <CardHeader title="BIM readiness" description={bim.note} />
          <div className="space-y-3 p-4">
            <Badge tone="neutral" icon={CircleSlash}>
              Not implemented · Phase {bim.phase}
            </Badge>
            <div>
              <p className="label">Interfaces defined</p>
              <div className="mt-1 flex flex-wrap gap-1">
                {bim.interfaces_defined.map((item) => (
                  <Badge key={item} tone="info">
                    {item}
                  </Badge>
                ))}
              </div>
            </div>
            <div>
              <p className="label">Schema already carries</p>
              <ul className="mt-1 space-y-0.5 font-mono text-2xs text-ink-muted">
                {bim.schema_ready.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
            <div>
              <p className="label">Still required</p>
              <ul className="mt-1 list-inside list-disc space-y-0.5 text-xs text-warn">
                {bim.still_required.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            </div>
          </div>
        </Card>

        <Card>
          <CardHeader title="Processing configuration" />
          <div className="grid gap-4 p-4 sm:grid-cols-2">
            <Metric
              label="Queue backend"
              value={processing.queue_backend}
              provenance="manual"
              hint={
                processing.queue_backend === 'thread'
                  ? 'In-process; not durable across restarts'
                  : 'Durable, multi-worker'
              }
            />
            <Metric
              label="Frame interval"
              value={`${processing.default_frame_interval_seconds}s`}
              provenance="manual"
              hint="Default for 360 video"
            />
            <Metric
              label="Max frames / video"
              value={processing.max_frames_per_video}
              provenance="manual"
            />
            <Metric
              label="Mapping threshold"
              value={`${Math.round(mapping_confidence_threshold * 100)}%`}
              provenance="manual"
              hint="Below this, manual placement is required"
            />
            <Metric
              label="Max upload"
              value={formatBytes(uploads.max_bytes)}
              provenance="manual"
            />
            <div>
              <p className="label">Accepted files</p>
              <div className="mt-1 flex flex-wrap gap-1">
                {[...uploads.image_extensions, ...uploads.video_extensions].map((extension) => (
                  <Badge key={extension} tone="neutral">
                    {extension}
                  </Badge>
                ))}
              </div>
            </div>
          </div>
        </Card>
      </div>
    </div>
  )
}

// ---- jobs (§45) -------------------------------------------------------------
function JobsTab() {
  const [status, setStatus] = useState('')
  const jobs = useJobs(status ? { status } : {})
  const summary = useJobSummary()

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {Object.entries(summary.data?.by_status ?? {}).map(([key, count]) => (
          <Card key={key} className="p-3.5">
            <Metric label={humanize(key)} value={count} provenance="measured" />
          </Card>
        ))}
      </div>

      {summary.data?.queue ? (
        <Callout
          tone={summary.data.queue.durable ? 'info' : 'warn'}
          title={`Queue: ${String(summary.data.queue.backend)}`}
        >
          {String(summary.data.queue.note ?? '')}
          {summary.data.queue.durable
            ? ' Queued work survives a restart.'
            : ''}
        </Callout>
      ) : null}

      <Card>
        <CardHeader
          title="Processing jobs"
          description="One row per pipeline stage attempt"
          actions={
            <Select
              value={status}
              onChange={(event) => setStatus(event.target.value)}
              className="w-36"
              aria-label="Job status"
            >
              <option value="">Any status</option>
              {['queued', 'running', 'complete', 'failed', 'skipped'].map((value) => (
                <option key={value} value={value}>
                  {humanize(value)}
                </option>
              ))}
            </Select>
          }
        />
        {jobs.isLoading ? (
          <div className="space-y-2 p-4">
            {[0, 1, 2].map((index) => (
              <Skeleton key={index} className="h-8" />
            ))}
          </div>
        ) : !jobs.data?.items.length ? (
          <EmptyState icon={Cpu} title="No jobs" />
        ) : (
          <Table>
            <thead>
              <tr>
                <Th>Capture</Th>
                <Th>Stage</Th>
                <Th>Status</Th>
                <Th className="text-right">Attempt</Th>
                <Th className="text-right">Duration</Th>
                <Th>Finished</Th>
                <Th>Error</Th>
              </tr>
            </thead>
            <tbody>
              {jobs.data.items.map((job) => (
                <Tr key={job.id}>
                  <Td className="font-mono text-2xs">{job.session_id}</Td>
                  <Td className="text-xs">{humanize(job.job_type)}</Td>
                  <Td>
                    <JobStatusBadge status={job.status} />
                  </Td>
                  <Td className="tnum text-right text-xs">
                    {job.attempt}/{job.max_attempts}
                  </Td>
                  <Td className="tnum text-right text-xs">
                    {formatDuration(job.duration_seconds)}
                  </Td>
                  <Td className="text-xs text-ink-muted">{formatDateTime(job.finished_at)}</Td>
                  <Td className="max-w-xs truncate text-xs text-bad">{job.error_detail ?? '—'}</Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </Card>

      {summary.data?.mean_duration_seconds &&
      Object.keys(summary.data.mean_duration_seconds).length ? (
        <Card>
          <CardHeader title="Mean stage duration" description="Completed jobs only" />
          <div className="grid gap-3 p-4 sm:grid-cols-3 lg:grid-cols-4">
            {Object.entries(summary.data.mean_duration_seconds).map(([stage, seconds]) => (
              <Metric
                key={stage}
                label={humanize(stage)}
                value={formatDuration(seconds)}
                provenance="measured"
              />
            ))}
          </div>
        </Card>
      ) : null}
    </div>
  )
}

// ---- users ------------------------------------------------------------------
function UsersTab() {
  const { can } = useAuth()
  const users = useUsers(can(Capability.UserManage))

  return (
    <Card>
      <CardHeader title="Users" description="Roles follow the §27 permission matrix" />
      {users.isLoading ? (
        <div className="p-4">
          <Skeleton className="h-10" />
        </div>
      ) : !users.data?.items.length ? (
        <EmptyState icon={Users} title="No users" />
      ) : (
        <Table>
          <thead>
            <tr>
              <Th>Name</Th>
              <Th>Email</Th>
              <Th>Role</Th>
              <Th>Last sign-in</Th>
              <Th>Active</Th>
            </tr>
          </thead>
          <tbody>
            {users.data.items.map((user) => (
              <Tr key={user.id}>
                <Td className="font-medium">{user.full_name}</Td>
                <Td className="text-xs text-ink-muted">{user.email}</Td>
                <Td>
                  <Badge tone={user.role === 'admin' ? 'brand' : 'neutral'}>
                    {humanize(user.role)}
                  </Badge>
                </Td>
                <Td className="text-xs text-ink-muted">{formatDateTime(user.last_login_at)}</Td>
                <Td>
                  <Badge tone={user.is_active ? 'good' : 'bad'}>
                    {user.is_active ? 'Active' : 'Deactivated'}
                  </Badge>
                </Td>
              </Tr>
            ))}
          </tbody>
        </Table>
      )}
    </Card>
  )
}
