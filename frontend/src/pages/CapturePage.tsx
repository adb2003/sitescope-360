/**
 * Site capture (§5): the mobile-friendly capture workflow plus the capture list.
 *
 * This is the web capture surface. It is the *same workflow* the Expo app in `mobile/` runs —
 * select project → building → floor → zone, start a session, add files, finish — and it works on
 * a phone browser today without an app install. It is deliberately usable one-handed: large
 * targets, a single column on narrow screens, and a queue that survives a dropped connection.
 */

import {
  Camera,
  CheckCircle2,
  CloudUpload,
  FileVideo,
  Images,
  Play,
  Plus,
  RefreshCw,
  Trash2,
  WifiOff,
  X,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { PageBody, PageHeader } from '@/components/layout/AppShell'
import { MappingConfidenceBadge, SessionStatusBadge } from '@/components/status'
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
  Td,
  Th,
  Tr,
} from '@/components/ui'
import {
  useCameras,
  useCaptures,
  useCreateCaptureSession,
  useProcessCapture,
  useProjectFloors,
  useUploadCaptureFiles,
  useZones,
  type UploadProgress,
} from '@/hooks/queries'
import { Capability, useAuth } from '@/hooks/useAuth'
import { useProjectContext } from '@/hooks/useProjectContext'
import { ApiError } from '@/lib/api'
import { DIRECT_UPLOAD_MIN_BYTES, routeFor } from '@/lib/uploadStrategy'
import { useUploadQueue } from '@/hooks/useUploadQueue'
import { cn, formatBytes, formatDateTime, formatRelative } from '@/lib/utils'

export function CapturePage() {
  const { projectId } = useProjectContext()
  const { can } = useAuth()
  const navigate = useNavigate()

  const canCapture = can(Capability.CaptureCreate)
  const captures = useCaptures(projectId ? { project_id: projectId } : {})

  if (!projectId) {
    return (
      <PageBody>
        <Card>
          <EmptyState
            icon={Camera}
            title="No project selected"
            description="Choose a project from the sidebar before starting a capture."
            action={<Button onClick={() => navigate('/projects')}>Browse projects</Button>}
          />
        </Card>
      </PageBody>
    )
  }

  return (
    <>
      <PageHeader
        title="Site capture"
        subtitle="Record on any 360 camera, then import the files here"
      />

      <PageBody className="space-y-4">
        {canCapture ? <CaptureWizard projectId={projectId} /> : null}

        <Card>
          <CardHeader
            title="Capture sessions"
            description="Every visit, newest first"
            actions={
              <Button
                size="sm"
                variant="ghost"
                icon={RefreshCw}
                onClick={() => void captures.refetch()}
              >
                Refresh
              </Button>
            }
          />
          {captures.isLoading ? (
            <div className="space-y-2 p-4">
              {[0, 1, 2].map((index) => (
                <Skeleton key={index} className="h-9" />
              ))}
            </div>
          ) : !captures.data?.items.length ? (
            <EmptyState
              icon={Images}
              title="No captures yet"
              description="Start a capture above, add your 360 files, then run processing."
            />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Session</Th>
                  <Th>Started</Th>
                  <Th>Status</Th>
                  <Th className="text-right">Files</Th>
                  <Th className="text-right">Frames</Th>
                  <Th>Mapping</Th>
                  <Th className="w-20" />
                </tr>
              </thead>
              <tbody>
                {captures.data.items.map((session) => (
                  <Tr key={session.id}>
                    <Td>
                      <button
                        type="button"
                        onClick={() => navigate(`/capture/${session.id}`)}
                        className="text-left"
                      >
                        <span className="block font-mono text-2xs text-brand hover:underline">
                          {session.id}
                        </span>
                        <span className="block truncate text-xs text-ink-muted">
                          {session.name ?? 'Unnamed capture'}
                        </span>
                      </button>
                    </Td>
                    <Td className="text-xs text-ink-muted">
                      {formatDateTime(session.started_at)}
                      <span className="ml-1.5 text-ink-subtle">
                        ({formatRelative(session.started_at)})
                      </span>
                    </Td>
                    <Td>
                      <SessionStatusBadge status={session.status} />
                    </Td>
                    <Td className="tnum text-right">{session.file_count}</Td>
                    <Td className="tnum text-right">{session.frame_count}</Td>
                    <Td>
                      <MappingConfidenceBadge
                        confidence={session.mapping_confidence}
                        score={session.mapping_confidence_score}
                      />
                    </Td>
                    <Td>
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => navigate(`/capture/${session.id}`)}
                      >
                        Open
                      </Button>
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>

        <Callout tone="info" title="Camera independence">
          Any 360 camera works: record, copy the files off the camera, and import them here. No
          camera SDK is required and no vendor account is involved. Registered devices with a
          folder path can also be imported directly — see Settings → Cameras.
        </Callout>
      </PageBody>
    </>
  )
}

// ---- capture wizard ---------------------------------------------------------
type Step = 'select' | 'files' | 'processing'

function CaptureWizard({ projectId }: { projectId: string }) {
  const navigate = useNavigate()
  const floors = useProjectFloors(projectId)
  const [floorId, setFloorId] = useState('')
  const zones = useZones(floorId || undefined)
  const cameras = useCameras()

  const createSession = useCreateCaptureSession()
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [progress, setProgress] = useState<UploadProgress | null>(null)
  const upload = useUploadCaptureFiles(sessionId ?? '', setProgress)
  const process = useProcessCapture(sessionId ?? '')

  const [step, setStep] = useState<Step>('select')
  const [error, setError] = useState<string | null>(null)
  const [selectedFiles, setSelectedFiles] = useState<File[]>([])
  const fileInput = useRef<HTMLInputElement>(null)
  const queue = useUploadQueue(sessionId)

  const online = useOnlineStatus()

  const totalBytes = useMemo(
    () => selectedFiles.reduce((sum, file) => sum + file.size, 0),
    [selectedFiles],
  )

  const directCount = useMemo(
    () => selectedFiles.filter((file) => routeFor(file.size) === 'direct').length,
    [selectedFiles],
  )

  async function startSession(form: FormData) {
    setError(null)
    try {
      const session = await createSession.mutateAsync({
        floor_id: String(form.get('floor_id')),
        zone_id: emptyToUndefined(form.get('zone_id')),
        name: emptyToUndefined(form.get('name')),
        camera_device_id: emptyToUndefined(form.get('camera_device_id')),
        camera_label: emptyToUndefined(form.get('camera_label')),
        frame_interval_seconds: form.get('frame_interval_seconds')
          ? Number(form.get('frame_interval_seconds'))
          : undefined,
        notes: emptyToUndefined(form.get('notes')),
      })
      setSessionId(session.id)
      setStep('files')
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not start the capture session')
    }
  }

  async function uploadNow() {
    if (!sessionId || !selectedFiles.length) return
    setError(null)
    try {
      await upload.mutateAsync(selectedFiles)
      setSelectedFiles([])
      queue.clear()
      if (fileInput.current) fileInput.current.value = ''
    } catch (caught) {
      // Queue the files rather than losing them — the §32 offline-first requirement.
      queue.enqueue(selectedFiles)
      setError(
        caught instanceof ApiError
          ? `${caught.message} — the files are queued and will retry.`
          : 'Upload failed. The files are queued and will retry when the connection returns.',
      )
    }
  }

  async function startProcessing() {
    if (!sessionId) return
    setError(null)
    try {
      await process.mutateAsync({})
      setStep('processing')
      navigate(`/capture/${sessionId}`)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Could not start processing')
    }
  }

  return (
    <Card>
      <CardHeader
        title="New capture"
        description="Project → Building → Floor → Zone, then add the recorded files"
        actions={
          <div className="flex items-center gap-1.5">
            <StepDot active={step === 'select'} done={step !== 'select'} label="1 Session" />
            <StepDot active={step === 'files'} done={step === 'processing'} label="2 Files" />
            <StepDot active={step === 'processing'} label="3 Process" />
          </div>
        }
      />

      <div className="p-4">
        {!online ? (
          <Callout tone="warn" title="You are offline" className="mb-3">
            <span className="flex items-center gap-1.5">
              <WifiOff className="size-3" aria-hidden />
              Files you add are queued locally and upload automatically when the connection
              returns. Nothing is lost.
            </span>
          </Callout>
        ) : null}

        {queue.items.length ? (
          <Callout tone="info" title={`${queue.items.length} file(s) waiting for upload`} className="mb-3">
            <div className="mt-1 flex flex-wrap items-center gap-2">
              {queue.items.slice(0, 6).map((item) => (
                <Badge key={item.id} tone="neutral">
                  {item.name}
                </Badge>
              ))}
              <Button
                size="sm"
                variant="secondary"
                icon={CloudUpload}
                loading={queue.flushing}
                onClick={() => void queue.flush()}
                disabled={!online}
              >
                Retry now
              </Button>
              <Button size="sm" variant="ghost" icon={Trash2} onClick={queue.clear}>
                Discard
              </Button>
            </div>
          </Callout>
        ) : null}

        {step === 'select' ? (
          <form
            onSubmit={(event) => {
              event.preventDefault()
              void startSession(new FormData(event.currentTarget))
            }}
            className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3"
          >
            <Field label="Floor" required hint="Determines the project and building">
              <Select
                name="floor_id"
                required
                value={floorId}
                onChange={(event) => setFloorId(event.target.value)}
              >
                <option value="">Select a floor</option>
                {floors.data?.map((floor) => (
                  <option key={floor.id} value={floor.id}>
                    {floor.building_name} · {floor.name}
                  </option>
                ))}
              </Select>
            </Field>

            <Field label="Zone" hint="Optional">
              <Select name="zone_id" disabled={!floorId}>
                <option value="">Whole floor</option>
                {zones.data?.map((zone) => (
                  <option key={zone.id} value={zone.id}>
                    {zone.name}
                  </option>
                ))}
              </Select>
            </Field>

            <Field label="Session name" hint="Defaults to the date and time">
              <Input name="name" placeholder="10 September — Morning walkthrough" />
            </Field>

            <Field label="Camera device" hint="Optional; registered under Settings">
              <Select name="camera_device_id">
                <option value="">Not specified</option>
                {cameras.data
                  ?.filter((device) => device.is_active)
                  .map((device) => (
                    <option key={device.id} value={device.id}>
                      {device.label}
                    </option>
                  ))}
              </Select>
            </Field>

            <Field label="Camera label" hint="Free text, if the device is not registered">
              <Input name="camera_label" placeholder="Insta360 X3" />
            </Field>

            <Field
              label="Video frame interval (seconds)"
              hint="Only used for 360 video. Leave blank for the default."
            >
              <Input
                name="frame_interval_seconds"
                type="number"
                step="0.5"
                min={0.5}
                max={60}
                placeholder="2"
              />
            </Field>

            <div className="sm:col-span-2 lg:col-span-3">
              <Field label="Notes">
                <Input name="notes" placeholder="Anything the office should know about this visit" />
              </Field>
            </div>

            {error ? (
              <div className="sm:col-span-2 lg:col-span-3">
                <Callout tone="bad">{error}</Callout>
              </div>
            ) : null}

            <div className="sm:col-span-2 lg:col-span-3">
              <Button
                type="submit"
                variant="primary"
                size="lg"
                icon={Play}
                loading={createSession.isPending}
                disabled={!floorId}
                className="w-full sm:w-auto"
              >
                Start capture
              </Button>
            </div>
          </form>
        ) : null}

        {step === 'files' && sessionId ? (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2 rounded border border-line bg-surface-raised px-3 py-2">
              <CheckCircle2 className="size-4 text-good" aria-hidden />
              <span className="text-sm text-ink">Session started</span>
              <code className="font-mono text-2xs text-ink-muted">{sessionId}</code>
            </div>

            <Field
              label="360 images or video"
              hint="JPG, PNG, MP4 or MOV. Multiple files are allowed."
            >
              <Input
                ref={fileInput}
                type="file"
                multiple
                accept=".jpg,.jpeg,.png,.mp4,.mov"
                onChange={(event) => setSelectedFiles(Array.from(event.target.files ?? []))}
              />
            </Field>

            {selectedFiles.length ? (
              <div className="rounded border border-line">
                <div className="flex items-center justify-between border-b border-line px-3 py-1.5">
                  <p className="text-xs font-medium text-ink">
                    {selectedFiles.length} file(s) · {formatBytes(totalBytes)}
                  </p>
                  <Button
                    size="sm"
                    variant="ghost"
                    icon={X}
                    onClick={() => {
                      setSelectedFiles([])
                      if (fileInput.current) fileInput.current.value = ''
                    }}
                  >
                    Clear
                  </Button>
                </div>
                <ul className="max-h-40 divide-y divide-line overflow-y-auto">
                  {selectedFiles.map((file) => (
                    <li
                      key={`${file.name}-${file.size}`}
                      className="flex items-center gap-2 px-3 py-1.5 text-xs"
                    >
                      {file.type.startsWith('video') ? (
                        <FileVideo className="size-3.5 shrink-0 text-info" aria-hidden />
                      ) : (
                        <Images className="size-3.5 shrink-0 text-brand" aria-hidden />
                      )}
                      <span className="min-w-0 flex-1 truncate text-ink">{file.name}</span>
                      {routeFor(file.size) === 'direct' ? (
                        <span
                          className="shrink-0 rounded bg-info/10 px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide text-info"
                          title={`Over ${formatBytes(DIRECT_UPLOAD_MIN_BYTES)} — uploads straight to storage instead of through the API`}
                        >
                          direct
                        </span>
                      ) : null}
                      <span className="tnum shrink-0 text-ink-subtle">
                        {formatBytes(file.size)}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}

            {directCount && !upload.isPending ? (
              <p className="text-xs text-ink-subtle">
                {directCount} of {selectedFiles.length} file(s) are over{' '}
                {formatBytes(DIRECT_UPLOAD_MIN_BYTES)} and will upload straight to storage. The
                server still checks the type and size afterwards and discards anything that fails.
              </p>
            ) : null}

            {progress ? (
              <div className="rounded border border-line px-3 py-2">
                <div className="flex items-baseline justify-between gap-2 text-xs">
                  <span className="min-w-0 truncate text-ink">
                    {progress.route === 'direct' ? 'Uploading to storage' : 'Uploading'} ·{' '}
                    {progress.filename}
                  </span>
                  <span className="tnum shrink-0 text-ink-subtle">
                    {progress.count > 1 ? `${progress.index} of ${progress.count} · ` : ''}
                    {formatBytes(progress.transferred)} / {formatBytes(progress.total)}
                  </span>
                </div>
                <div className="mt-1.5 h-1 overflow-hidden rounded bg-line">
                  <div
                    className="h-full rounded bg-brand transition-[width] duration-150"
                    style={{
                      width: `${progress.total ? Math.min(100, (progress.transferred / progress.total) * 100) : 0}%`,
                    }}
                  />
                </div>
              </div>
            ) : null}

            {error ? <Callout tone="bad">{error}</Callout> : null}

            <div className="flex flex-wrap gap-2">
              <Button
                variant="primary"
                icon={CloudUpload}
                loading={upload.isPending}
                disabled={!selectedFiles.length}
                onClick={() => void uploadNow()}
              >
                Upload {selectedFiles.length ? `${selectedFiles.length} file(s)` : ''}
              </Button>
              <Button variant="secondary" icon={Plus} onClick={() => fileInput.current?.click()}>
                Add more
              </Button>
              <Button
                variant="secondary"
                loading={process.isPending}
                onClick={() => void startProcessing()}
              >
                Finish and process
              </Button>
              <Button variant="ghost" onClick={() => navigate(`/capture/${sessionId}`)}>
                Open capture detail
              </Button>
            </div>

            <Callout tone="info" title="What happens on processing">
              Files are validated, metadata extracted, video split into frames at the configured
              interval, each frame scored for quality, features detected, and relative positions
              estimated. Processing runs in the background — you can close this page.
            </Callout>
          </div>
        ) : null}
      </div>
    </Card>
  )
}

function StepDot({ active, done, label }: { active?: boolean; done?: boolean; label: string }) {
  return (
    <span
      className={cn(
        'rounded px-1.5 py-0.5 text-2xs font-semibold',
        active
          ? 'bg-brand text-brand-ink'
          : done
            ? 'bg-good/15 text-good'
            : 'bg-surface-raised text-ink-subtle',
      )}
    >
      {label}
    </span>
  )
}

function emptyToUndefined(value: FormDataEntryValue | null): string | undefined {
  const text = String(value ?? '').trim()
  return text ? text : undefined
}

/** Tracks connectivity so the capture UI can say plainly that work is being queued. */
function useOnlineStatus(): boolean {
  const [online, setOnline] = useState(() => navigator.onLine)
  const onChange = useCallback(() => setOnline(navigator.onLine), [])

  useEffect(() => {
    window.addEventListener('online', onChange)
    window.addEventListener('offline', onChange)
    return () => {
      window.removeEventListener('online', onChange)
      window.removeEventListener('offline', onChange)
    }
  }, [onChange])

  return online
}
