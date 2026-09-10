/**
 * Upload queue (§32): "3 captures waiting for upload".
 *
 * Shows exactly what is still on the device and why, and lets the operator push it when they
 * have signal. Nothing is ever silently discarded — a failed upload keeps its file and its error
 * message so a site engineer can see that their work is still safe.
 */

import { useCallback, useEffect, useState } from 'react'
import { RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native'

import { Badge, Body, Button, Callout, Card, Divider, Heading, Label } from '../components/ui'
import {
  discardUploaded,
  listFiles,
  listSessions,
  requestProcessing,
  sync,
  type PendingFile,
  type PendingSession,
} from '../lib/queue'
import { spacing, theme } from '../theme'

export function QueueScreen({
  online,
  onQueueChanged,
}: {
  online: boolean
  onQueueChanged: () => void
}) {
  const [sessions, setSessions] = useState<PendingSession[]>([])
  const [files, setFiles] = useState<PendingFile[]>([])
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState<string | null>(null)
  const [errors, setErrors] = useState<string[]>([])

  const load = useCallback(async () => {
    const [loadedSessions, loadedFiles] = await Promise.all([listSessions(), listFiles()])
    setSessions(loadedSessions)
    setFiles(loadedFiles)
    onQueueChanged()
  }, [onQueueChanged])

  useEffect(() => {
    void load()
  }, [load])

  async function pushNow() {
    setBusy(true)
    setMessage(null)
    setErrors([])
    try {
      const result = await sync()
      setMessage(
        `${result.sessionsCreated} session(s) registered, ${result.filesUploaded} file(s) uploaded` +
          (result.filesFailed ? `, ${result.filesFailed} failed` : ''),
      )
      setErrors(result.errors)
    } finally {
      await load()
      setBusy(false)
    }
  }

  async function process(sessionId: string) {
    setBusy(true)
    setMessage(null)
    try {
      await requestProcessing(sessionId)
      setMessage(`Processing started for ${sessionId}. Track it in the web dashboard.`)
    } catch (caught) {
      setMessage(caught instanceof Error ? caught.message : 'Could not start processing')
    } finally {
      setBusy(false)
    }
  }

  const pending = files.filter((file) => file.status === 'pending' || file.status === 'failed')
  const uploaded = files.filter((file) => file.status === 'uploaded')

  return (
    <ScrollView
      style={styles.flex}
      contentContainerStyle={styles.content}
      refreshControl={
        <RefreshControl refreshing={busy} onRefresh={() => void load()} tintColor={theme.brand} />
      }
    >
      <Card>
        <Heading>
          {pending.length
            ? `${pending.length} file(s) waiting for upload`
            : 'Everything is uploaded'}
        </Heading>

        {!online ? (
          <Callout tone="warn" title="Offline">
            Nothing can be sent right now. Your captures are safe on this device.
          </Callout>
        ) : null}

        {message ? <Callout tone="info">{message}</Callout> : null}

        {errors.length ? (
          <Callout tone="bad" title="Some uploads did not complete">
            {errors.slice(0, 4).join('\n')}
          </Callout>
        ) : null}

        <Button
          label="Upload now"
          variant="primary"
          onPress={pushNow}
          loading={busy}
          disabled={!online || pending.length === 0}
        />

        {uploaded.length ? (
          <>
            <View style={{ height: spacing.sm }} />
            <Button
              label={`Clear ${uploaded.length} uploaded file(s) from the device`}
              onPress={async () => {
                await discardUploaded()
                await load()
              }}
            />
          </>
        ) : null}
      </Card>

      {sessions.length ? (
        <Card>
          <Heading>Capture sessions on this device</Heading>
          {sessions.map((session) => {
            const sessionFiles = files.filter((file) => file.sessionId === session.id)
            const outstanding = sessionFiles.filter((file) => file.status !== 'uploaded')
            const allUploaded = sessionFiles.length > 0 && outstanding.length === 0

            return (
              <View key={session.id} style={styles.session}>
                <View style={styles.sessionHeader}>
                  <Text style={styles.sessionId} numberOfLines={1}>
                    {session.id}
                  </Text>
                  <Badge
                    label={session.synced ? 'registered' : 'local only'}
                    tone={session.synced ? 'good' : 'warn'}
                  />
                </View>

                <Text style={styles.sessionMeta}>
                  {new Date(session.startedAt).toLocaleString()}
                  {session.endedAt ? ' · finished' : ' · not finished'}
                </Text>
                <Text style={styles.sessionMeta}>
                  {sessionFiles.length} file(s)
                  {outstanding.length ? ` · ${outstanding.length} waiting` : ' · all uploaded'}
                </Text>

                {sessionFiles.map((file) => (
                  <View key={file.id} style={styles.fileRow}>
                    <Text style={styles.fileName} numberOfLines={1}>
                      {file.name}
                    </Text>
                    <Badge
                      label={file.status}
                      tone={
                        file.status === 'uploaded'
                          ? 'good'
                          : file.status === 'failed'
                            ? 'bad'
                            : file.status === 'uploading'
                              ? 'brand'
                              : 'neutral'
                      }
                    />
                  </View>
                ))}

                {sessionFiles.some((file) => file.lastError) ? (
                  <Text style={styles.errorText}>
                    {sessionFiles.find((file) => file.lastError)?.lastError}
                  </Text>
                ) : null}

                {allUploaded && session.synced ? (
                  <>
                    <View style={{ height: spacing.sm }} />
                    <Button
                      label="Start processing"
                      onPress={() => void process(session.id)}
                      disabled={!online || busy}
                    />
                  </>
                ) : null}

                <Divider />
              </View>
            )
          })}
        </Card>
      ) : (
        <Card>
          <Body muted>No captures on this device yet.</Body>
        </Card>
      )}

      <Callout tone="info" title="Why files stay on the device">
        A file is deleted from the phone only once the server has confirmed it. If an upload fails
        halfway, the file is still here and the next attempt starts again — a capture is never
        lost to a dropped connection.
      </Callout>

      <Label>Processing happens on the server</Label>
      <Body muted>
        Frame extraction, quality scoring and mapping all run in the backend pipeline. This app
        uploads and reports; it does not process imagery on the phone.
      </Body>
    </ScrollView>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1, backgroundColor: theme.canvas },
  content: { padding: spacing.lg, gap: spacing.lg, paddingBottom: spacing.xl * 2 },
  session: { marginTop: spacing.md },
  sessionHeader: { flexDirection: 'row', alignItems: 'center', gap: spacing.sm },
  sessionId: { color: theme.ink, fontSize: 13, fontFamily: 'monospace', flex: 1 },
  sessionMeta: { color: theme.inkSubtle, fontSize: 12, marginTop: 2 },
  fileRow: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.sm,
    paddingVertical: spacing.xs,
  },
  fileName: { color: theme.inkMuted, fontSize: 13, flex: 1 },
  errorText: { color: theme.bad, fontSize: 12, marginTop: spacing.xs },
})
