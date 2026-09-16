/**
 * The §5 capture workflow: select project → building → floor → zone, start, record, finish.
 *
 * The screen is deliberately usable with no connectivity, and that now holds for choosing *where*
 * as well as for recording. The project tree is read from the device's own cache — filled from
 * `/sync` whenever there is a connection, see `lib/refData.ts` — rather than from three live
 * requests, which used to stand between the operator and starting a capture on exactly the floors
 * where there is no signal to answer them. A session is then created **locally first** with a
 * device-generated id, so "Start capture" never waits on the network and never fails because
 * of it.
 */

import * as DocumentPicker from 'expo-document-picker'
import * as ImagePicker from 'expo-image-picker'
import * as Location from 'expo-location'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Pressable, ScrollView, StyleSheet, Text, View } from 'react-native'

import { Badge, Body, Button, Callout, Card, Divider, Field, Heading, Input, Label } from '../components/ui'
import {
  snapshotFileUrls,
  startRecording,
  stopRecording,
  transferToQueue,
  type TransferProgress,
} from '../lib/cameraCapture'
import { CameraError } from '../lib/osc'
import { useCamera } from '../lib/useCamera'
import {
  cachedFloors,
  cachedProjects,
  cachedZones,
  hasCachedReferenceData,
  syncReferenceData,
} from '../lib/refData'
import {
  QUEUE_IS_AVAILABLE,
  createLocalSession,
  enqueueFile,
  finishLocalSession,
  listFiles,
  newSessionId,
  type PendingFile,
} from '../lib/queue'
import { spacing, theme } from '../theme'

interface Floor {
  id: string
  name: string
  building_name: string
  project_id: string
}

interface Zone {
  id: string
  name: string
}

type Phase = 'setup' | 'recording' | 'finished'

export function CaptureScreen({
  online,
  onQueueChanged,
  onOpenQueue,
  onOpenCamera,
}: {
  online: boolean
  onQueueChanged: () => void
  onOpenQueue: () => void
  onOpenCamera: () => void
}) {
  const { connection: camera } = useCamera()
  const [cameraBusy, setCameraBusy] = useState(false)
  const [transfer, setTransfer] = useState<TransferProgress | null>(null)
  // File URLs already on the camera when recording began, so a camera that finalises its file
  // asynchronously can still be matched to the one new recording.
  const [urlsBeforeRecording, setUrlsBeforeRecording] = useState<string[]>([])
  const [projects, setProjects] = useState<Array<{ id: string; name: string; code: string }>>([])
  const [floors, setFloors] = useState<Floor[]>([])
  const [zones, setZones] = useState<Zone[]>([])

  const [projectId, setProjectId] = useState<string | null>(null)
  const [floorId, setFloorId] = useState<string | null>(null)
  const [zoneId, setZoneId] = useState<string | null>(null)
  const [cameraLabel, setCameraLabel] = useState('')
  const [notes, setNotes] = useState('')

  const [phase, setPhase] = useState<Phase>('setup')
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [startedAt, setStartedAt] = useState<Date | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const [paused, setPaused] = useState(false)
  const [files, setFiles] = useState<PendingFile[]>([])
  const [message, setMessage] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [coords, setCoords] = useState<{ latitude: number; longitude: number } | null>(null)

  // ---- reference data, from the device -----------------------------------
  // Read from the local cache, always. Connectivity refreshes it and is never a precondition:
  // three live requests used to stand between the operator and starting a capture, on exactly
  // the floors where there is no signal to answer them.
  const [refreshedAt, setRefreshedAt] = useState(0)
  const [neverSynced, setNeverSynced] = useState(false)

  useEffect(() => {
    if (!QUEUE_IS_AVAILABLE) return
    void (async () => {
      setProjects(await cachedProjects())
      setNeverSynced(!(await hasCachedReferenceData()))
    })()
  }, [refreshedAt])

  useEffect(() => {
    if (!QUEUE_IS_AVAILABLE || !projectId) {
      setFloors([])
      return
    }
    void (async () => setFloors(await cachedFloors(projectId)))()
  }, [projectId, refreshedAt])

  useEffect(() => {
    if (!QUEUE_IS_AVAILABLE || !floorId) {
      setZones([])
      return
    }
    void (async () => setZones(await cachedZones(floorId)))()
  }, [floorId, refreshedAt])

  // Refresh when there is a connection to refresh from. A failure is silent on purpose: the
  // cache is what the screen runs on, and an operator who is offline has nothing to act on.
  useEffect(() => {
    if (!online || !QUEUE_IS_AVAILABLE) return
    void (async () => {
      try {
        await syncReferenceData()
        setRefreshedAt(Date.now())
      } catch {
        // Keep showing what is cached.
      }
    })()
  }, [online])

  // ---- elapsed timer ----------------------------------------------------
  useEffect(() => {
    if (phase !== 'recording' || paused || !startedAt) return
    const timer = setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt.getTime()) / 1000))
    }, 1000)
    return () => clearInterval(timer)
  }, [phase, paused, startedAt])

  const refreshFiles = useCallback(async () => {
    if (!sessionId) return
    setFiles(await listFiles(sessionId))
    onQueueChanged()
  }, [sessionId, onQueueChanged])

  // ---- actions ----------------------------------------------------------
  async function start() {
    if (!floorId) {
      setMessage('Select a floor first.')
      return
    }
    setBusy(true)
    setMessage(null)

    // Best-effort GPS. A refusal or a timeout must not stop the capture.
    let position: { latitude: number; longitude: number } | null = null
    try {
      const permission = await Location.requestForegroundPermissionsAsync()
      if (permission.granted) {
        const fix = await Location.getCurrentPositionAsync({
          accuracy: Location.Accuracy.Balanced,
        })
        position = { latitude: fix.coords.latitude, longitude: fix.coords.longitude }
        setCoords(position)
      }
    } catch {
      // No location; the session simply carries none.
    }

    const now = new Date()
    const id = newSessionId(now)
    await createLocalSession({
      id,
      floorId,
      zoneId,
      name: `${now.toLocaleDateString()} — ${now.toLocaleTimeString()}`,
      startedAt: now.toISOString(),
      endedAt: null,
      cameraLabel: cameraLabel.trim() || null,
      frameIntervalSeconds: null,
      notes: notes.trim() || null,
      latitude: position?.latitude ?? null,
      longitude: position?.longitude ?? null,
    })

    setSessionId(id)
    setStartedAt(now)
    setElapsed(0)
    setPaused(false)
    setPhase('recording')
    setBusy(false)
    onQueueChanged()
  }

  async function addFromLibrary() {
    if (!sessionId) return
    const permission = await ImagePicker.requestMediaLibraryPermissionsAsync()
    if (!permission.granted) {
      setMessage('Photo library access is needed to import captures.')
      return
    }
    const result = await ImagePicker.launchImageLibraryAsync({
      mediaTypes: ImagePicker.MediaTypeOptions.All,
      allowsMultipleSelection: true,
      quality: 1,
      // Never re-encode: re-compressing a 360 panorama loses detail the site team needs.
      exif: true,
    })
    if (result.canceled) return

    setBusy(true)
    for (const asset of result.assets) {
      await enqueueFile(sessionId, {
        uri: asset.uri,
        name: asset.fileName ?? `capture-${Date.now()}.jpg`,
        size: asset.fileSize,
        mimeType: asset.mimeType,
      })
    }
    await refreshFiles()
    setBusy(false)
  }

  async function addFromFiles() {
    if (!sessionId) return
    const result = await DocumentPicker.getDocumentAsync({
      type: ['image/jpeg', 'image/png', 'video/mp4', 'video/quicktime'],
      multiple: true,
      copyToCacheDirectory: true,
    })
    if (result.canceled) return

    setBusy(true)
    for (const asset of result.assets) {
      await enqueueFile(sessionId, {
        uri: asset.uri,
        name: asset.name,
        size: asset.size ?? undefined,
        mimeType: asset.mimeType ?? undefined,
      })
    }
    await refreshFiles()
    setBusy(false)
  }

  async function finish() {
    if (!sessionId) return
    await finishLocalSession(sessionId)
    setPhase('finished')
    onQueueChanged()
  }

  /**
   * Start the camera recording as part of starting the session.
   *
   * Failures here are reported but do not abort the session: the operator may be recording with
   * the camera's own button, and a session that refuses to start because a remote trigger failed
   * would be worse than one that simply is not driving the camera.
   */
  async function triggerCameraStart() {
    if (!camera) return
    setCameraBusy(true)
    setMessage(null)
    try {
      setUrlsBeforeRecording(await snapshotFileUrls(camera.client))
      await startRecording(camera.client)
      setMessage('Camera recording. Walk the path, then tap Finish.')
    } catch (caught) {
      setMessage(
        caught instanceof CameraError
          ? `Camera did not start: ${caught.message}`
          : 'Camera did not start. You can record with the camera itself and import afterwards.',
      )
    } finally {
      setCameraBusy(false)
    }
  }

  /**
   * Stop recording and pull the file across.
   *
   * Nothing is deleted from the camera. A transfer that fails, or an app that is killed
   * mid-copy, must never be able to destroy the only copy of a walkthrough -- so the recording
   * stays on the device until the operator removes it themselves.
   */
  async function stopAndTransfer() {
    if (!camera || !sessionId) return
    setCameraBusy(true)
    setMessage(null)
    try {
      const produced = await stopRecording(camera.client, urlsBeforeRecording)
      for (const file of produced) {
        await transferToQueue(camera.client, sessionId, file, setTransfer)
      }
      setTransfer(null)
      await refreshFiles()
      setMessage(
        produced.length === 1
          ? 'Recording transferred and queued for upload.'
          : `${produced.length} recordings transferred and queued for upload.`,
      )
    } catch (caught) {
      setTransfer(null)
      setMessage(
        caught instanceof CameraError
          ? caught.message
          : 'Could not transfer from the camera. The recording is still on the camera.',
      )
    } finally {
      setCameraBusy(false)
    }
  }

  function reset() {
    setPhase('setup')
    setSessionId(null)
    setStartedAt(null)
    setElapsed(0)
    setFiles([])
    setMessage(null)
    setTransfer(null)
    setUrlsBeforeRecording([])
  }

  const totalBytes = useMemo(
    () => files.reduce((sum, file) => sum + file.sizeBytes, 0),
    [files],
  )
  const selectedFloor = floors.find((floor) => floor.id === floorId)

  return (
    <ScrollView style={styles.flex} contentContainerStyle={styles.content}>
      {!online && !neverSynced ? (
        <Callout tone="warn" title="Offline">
          Everything below still works. The project list is the one downloaded on this device;
          sessions and files stay here and upload automatically when the connection returns.
        </Callout>
      ) : null}

      {/* The one case the device genuinely cannot cover. Worth saying plainly rather than
          showing an empty picker, which looks identical to a broken app. */}
      {!online && neverSynced ? (
        <Callout tone="bad" title="No project list on this device yet">
          The project tree is downloaded once and then kept, but this device has never had a
          connection to download it from. Find signal briefly and it will be here for every
          later visit, offline included.
        </Callout>
      ) : null}

      {message ? <Callout tone="info">{message}</Callout> : null}

      {phase === 'setup' ? (
        <Card>
          <Heading>New capture</Heading>

          <Field label="Project">
            <Chooser
              options={projects.map((project) => ({
                id: project.id,
                label: `${project.code} — ${project.name}`,
              }))}
              value={projectId}
              onChange={(id) => {
                setProjectId(id)
                setFloorId(null)
                setZoneId(null)
              }}
              emptyLabel={
                neverSynced
                  ? 'Connect once to download the project list'
                  : 'No projects on this account'
              }
            />
          </Field>

          <CameraStatus camera={camera} onOpenCamera={onOpenCamera} />

          <Field label="Floor">
            <Chooser
              options={floors.map((floor) => ({
                id: floor.id,
                label: `${floor.building_name} · ${floor.name}`,
              }))}
              value={floorId}
              onChange={(id) => {
                setFloorId(id)
                setZoneId(null)
              }}
              emptyLabel="Select a project first"
            />
          </Field>

          <Field label="Zone" hint="Optional">
            <Chooser
              options={zones.map((zone) => ({ id: zone.id, label: zone.name }))}
              value={zoneId}
              onChange={setZoneId}
              emptyLabel="Whole floor"
              allowClear
            />
          </Field>

          <Field label="Camera" hint="Free text, e.g. the model you are recording on">
            <Input value={cameraLabel} onChangeText={setCameraLabel} placeholder="360 camera" />
          </Field>

          <Field label="Notes" hint="Optional">
            <Input value={notes} onChangeText={setNotes} placeholder="Anything the office should know" />
          </Field>

          <Button
            label="Start capture"
            variant="primary"
            onPress={start}
            loading={busy}
            disabled={!floorId}
          />
        </Card>
      ) : null}

      {phase === 'recording' && sessionId ? (
        <>
          <Card>
            <View style={styles.recordingHeader}>
              <View style={[styles.dot, paused && { backgroundColor: theme.warn }]} />
              <Text style={styles.recordingText}>{paused ? 'Paused' : 'Recording…'}</Text>
              <Text style={styles.timer}>{formatElapsed(elapsed)}</Text>
            </View>

            <Divider />

            {/* The §5 status block. */}
            <View style={styles.statusGrid}>
              <Status label="Session" value={sessionId} mono />
              <Status label="Floor" value={selectedFloor ? `${selectedFloor.building_name} · ${selectedFloor.name}` : '—'} />
              <Status label="Zone" value={zones.find((zone) => zone.id === zoneId)?.name ?? 'Whole floor'} />
              <Status label="Camera" value={cameraLabel.trim() || 'Not specified'} />
              <Status label="Files" value={`${files.length} · ${formatBytes(totalBytes)}`} />
              <Status label="Connection" value={online ? 'Online' : 'Offline — queued locally'} />
              <Status
                label="GPS"
                value={
                  coords
                    ? `${coords.latitude.toFixed(5)}, ${coords.longitude.toFixed(5)}`
                    : 'Not available'
                }
              />
              <Status label="Storage" value="On device until uploaded" />
            </View>

            <Divider />

            {camera ? (
              <>
                <Label>Camera</Label>
                {transfer ? (
                  <View style={styles.transfer}>
                    <Text style={styles.transferLabel}>
                      Transferring {transfer.file} — {Math.round(transfer.fraction * 100)}%
                    </Text>
                    <View style={styles.transferTrack}>
                      <View
                        style={[styles.transferFill, { width: `${Math.round(transfer.fraction * 100)}%` }]}
                      />
                    </View>
                    <Text style={styles.transferHint}>
                      Stay on the camera&rsquo;s Wi-Fi until this finishes.
                    </Text>
                  </View>
                ) : (
                  <View style={styles.buttonRow}>
                    <Button
                      label="Start camera"
                      onPress={() => void triggerCameraStart()}
                      loading={cameraBusy}
                      style={styles.grow}
                    />
                    <Button
                      label="Stop &amp; transfer"
                      onPress={() => void stopAndTransfer()}
                      loading={cameraBusy}
                      style={styles.grow}
                    />
                  </View>
                )}
                <Divider />
              </>
            ) : null}

            <View style={styles.buttonRow}>
              <Button
                label={paused ? 'Resume' : 'Pause'}
                onPress={() => setPaused((value) => !value)}
                style={styles.grow}
              />
              <Button label="Finish" variant="primary" onPress={finish} style={styles.grow} />
            </View>
            <Button label="Cancel capture" variant="ghost" onPress={reset} />
          </Card>

          <Card>
            <Heading>Add recorded files</Heading>
            <Body muted>
              Record on your 360 camera, transfer the files to this phone, then add them here. No
              camera integration is required.
            </Body>
            <View style={{ height: spacing.md }} />
            <Button label="Add from photo library" onPress={addFromLibrary} loading={busy} />
            <View style={{ height: spacing.sm }} />
            <Button label="Add from files" onPress={addFromFiles} loading={busy} />

            {files.length ? (
              <>
                <Divider />
                <Label>Queued for this session</Label>
                {files.map((file) => (
                  <View key={file.id} style={styles.fileRow}>
                    <Text style={styles.fileName} numberOfLines={1}>
                      {file.name}
                    </Text>
                    <Text style={styles.fileSize}>{formatBytes(file.sizeBytes)}</Text>
                    <Badge
                      label={file.status}
                      tone={
                        file.status === 'uploaded'
                          ? 'good'
                          : file.status === 'failed'
                            ? 'bad'
                            : 'neutral'
                      }
                    />
                  </View>
                ))}
              </>
            ) : null}
          </Card>
        </>
      ) : null}

      {phase === 'finished' && sessionId ? (
        <Card>
          <Heading>Capture finished</Heading>
          <Status label="Session" value={sessionId} mono />
          <Status label="Duration" value={formatElapsed(elapsed)} />
          <Status label="Files" value={`${files.length} · ${formatBytes(totalBytes)}`} />

          <Callout tone={online ? 'good' : 'warn'} title={online ? 'Ready to upload' : 'Waiting for a connection'}>
            {online
              ? 'Open the upload queue to send this capture to the server, then start processing.'
              : 'This capture is safe on the device. It will upload automatically once you have signal.'}
          </Callout>

          <Button label="Open upload queue" variant="primary" onPress={onOpenQueue} />
          <View style={{ height: spacing.sm }} />
          <Button label="Start another capture" onPress={reset} />
        </Card>
      ) : null}
    </ScrollView>
  )
}

// ---- small pieces -----------------------------------------------------------
/**
 * Whether a camera is connected, stated before the operator starts walking.
 *
 * Deliberately not a blocker. Importing files the camera's own app already copied to the phone
 * remains a first-class path -- it is the only option for a camera that speaks no open protocol,
 * and the fallback when a connection drops on site.
 */
function CameraStatus({
  camera,
  onOpenCamera,
}: {
  camera: ReturnType<typeof useCamera>['connection']
  onOpenCamera: () => void
}) {
  if (!camera) {
    return (
      <View style={styles.cameraStatus}>
        <Callout tone="info" title="No camera connected">
          Connect one to start and stop recording from here and pull the file across
          automatically. You can also capture with the camera itself and import the files below.
        </Callout>
        <Button label="Connect a camera" onPress={onOpenCamera} />
      </View>
    )
  }

  const battery = camera.state.batteryLevel
  const percent = battery === null ? null : Math.round(battery * 100)
  return (
    <View style={styles.cameraStatus}>
      <View style={styles.cameraRow}>
        <Badge label="camera ready" tone="good" />
        <Text style={styles.cameraModel} numberOfLines={1}>
          {camera.info.model}
          {percent === null ? '' : ` · ${percent}%`}
        </Text>
      </View>
    </View>
  )
}

function Chooser({
  options,
  value,
  onChange,
  emptyLabel,
  allowClear,
}: {
  options: Array<{ id: string; label: string }>
  value: string | null
  onChange: (id: string | null) => void
  emptyLabel: string
  allowClear?: boolean
}) {
  if (!options.length) {
    return <Text style={styles.chooserEmpty}>{emptyLabel}</Text>
  }
  return (
    <View style={styles.chooser}>
      {allowClear ? (
        <Pressable
          onPress={() => onChange(null)}
          style={[styles.chooserItem, value === null && styles.chooserItemActive]}
        >
          <Text style={[styles.chooserText, value === null && styles.chooserTextActive]}>
            {emptyLabel}
          </Text>
        </Pressable>
      ) : null}
      {options.map((option) => {
        const active = option.id === value
        return (
          <Pressable
            key={option.id}
            onPress={() => onChange(option.id)}
            accessibilityRole="button"
            accessibilityState={{ selected: active }}
            style={[styles.chooserItem, active && styles.chooserItemActive]}
          >
            <Text style={[styles.chooserText, active && styles.chooserTextActive]}>
              {option.label}
            </Text>
          </Pressable>
        )
      })}
    </View>
  )
}

function Status({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <View style={styles.statusItem}>
      <Text style={styles.statusLabel}>{label}</Text>
      <Text style={[styles.statusValue, mono && styles.mono]} numberOfLines={1}>
        {value}
      </Text>
    </View>
  )
}

function formatElapsed(seconds: number): string {
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const rest = seconds % 60
  const pad = (value: number) => String(value).padStart(2, '0')
  return hours ? `${hours}:${pad(minutes)}:${pad(rest)}` : `${pad(minutes)}:${pad(rest)}`
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const units = ['KB', 'MB', 'GB']
  let value = bytes / 1024
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`
}

const styles = StyleSheet.create({
  cameraStatus: { gap: spacing.sm, marginBottom: spacing.md },
  cameraRow: { flexDirection: 'row', alignItems: 'center', gap: spacing.sm },
  cameraModel: { color: theme.inkMuted, flex: 1 },
  transfer: { gap: spacing.xs, marginVertical: spacing.sm },
  transferLabel: { color: theme.ink },
  transferTrack: {
    height: 6,
    borderRadius: 3,
    backgroundColor: theme.line,
    overflow: 'hidden',
  },
  transferFill: { height: 6, backgroundColor: theme.brand },
  transferHint: { color: theme.inkSubtle, fontSize: 12 },
  flex: { flex: 1, backgroundColor: theme.canvas },
  content: { padding: spacing.lg, gap: spacing.lg, paddingBottom: spacing.xl * 2 },
  recordingHeader: { flexDirection: 'row', alignItems: 'center', gap: spacing.sm },
  dot: { width: 10, height: 10, borderRadius: 5, backgroundColor: theme.bad },
  recordingText: { color: theme.ink, fontSize: 16, fontWeight: '600', flex: 1 },
  timer: { color: theme.ink, fontSize: 22, fontWeight: '700', fontVariant: ['tabular-nums'] },
  statusGrid: { flexDirection: 'row', flexWrap: 'wrap' },
  statusItem: { width: '50%', paddingVertical: spacing.xs, paddingRight: spacing.sm },
  statusLabel: {
    color: theme.inkSubtle,
    fontSize: 10,
    fontWeight: '600',
    letterSpacing: 0.5,
    textTransform: 'uppercase',
  },
  statusValue: { color: theme.ink, fontSize: 14 },
  mono: { fontFamily: 'monospace', fontSize: 12 },
  buttonRow: { flexDirection: 'row', gap: spacing.sm, marginBottom: spacing.sm },
  grow: { flex: 1 },
  chooser: { gap: spacing.xs },
  chooserItem: {
    minHeight: 44,
    justifyContent: 'center',
    paddingHorizontal: spacing.md,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: theme.line,
    backgroundColor: theme.surfaceRaised,
  },
  chooserItemActive: { borderColor: theme.brand, backgroundColor: '#142e2a' },
  chooserText: { color: theme.inkMuted, fontSize: 15 },
  chooserTextActive: { color: theme.brand, fontWeight: '600' },
  chooserEmpty: { color: theme.inkSubtle, fontSize: 14, paddingVertical: spacing.sm },
  fileRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.sm,
    paddingVertical: spacing.sm,
    borderBottomWidth: 1,
    borderBottomColor: theme.line,
  },
  fileName: { color: theme.ink, fontSize: 13, flex: 1 },
  fileSize: { color: theme.inkSubtle, fontSize: 12 },
})
