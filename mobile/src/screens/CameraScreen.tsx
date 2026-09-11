/**
 * Camera connection screen (§6, §7).
 *
 * The step the app was missing. Previously the operator had to use the camera vendor's own app to
 * get files onto the phone, then import them here — so the platform's capture flow began after
 * the interesting part was already over. This screen connects to the camera directly.
 *
 * The order on screen mirrors the order on site: join the Wi-Fi, connect, check the camera is
 * actually ready, then go to Capture.
 */

import { useCallback, useEffect, useState } from 'react'
import { RefreshControl, ScrollView, StyleSheet, Text, View } from 'react-native'

import { Badge, Body, Button, Callout, Card, Divider, Field, Heading, Input, Label } from '../components/ui'
import { KNOWN_CAMERA_HOSTS } from '../lib/osc'
import { useCamera } from '../lib/useCamera'
import { spacing, theme } from '../theme'

export function CameraScreen({ onReady }: { onReady: () => void }) {
  const { connection, connecting, error, connect, disconnect, refresh } = useCamera()
  const [manualHost, setManualHost] = useState('')
  const [refreshing, setRefreshing] = useState(false)

  // Poll while connected. A camera whose battery dies mid-walkthrough is the failure the
  // operator most needs warning about, and it is invisible unless asked for.
  useEffect(() => {
    if (!connection) return
    const timer = setInterval(() => void refresh(), 15_000)
    return () => clearInterval(timer)
  }, [connection, refresh])

  const onPullRefresh = useCallback(async () => {
    setRefreshing(true)
    await refresh()
    setRefreshing(false)
  }, [refresh])

  const battery = connection?.state.batteryLevel
  const batteryPercent = battery === null || battery === undefined ? null : Math.round(battery * 100)
  const lowBattery = batteryPercent !== null && batteryPercent < 25
  const recording = connection?.state.captureStatus === 'shooting'

  return (
    <ScrollView
      contentContainerStyle={styles.content}
      refreshControl={
        connection ? <RefreshControl refreshing={refreshing} onRefresh={onPullRefresh} /> : undefined
      }
    >
      <Heading>Camera</Heading>
      <Body muted>Connect to the 360 camera over its own Wi-Fi before starting a capture.</Body>

      {!connection ? (
        <>
          <Card>
            <Label>Before connecting</Label>
            <View style={styles.steps}>
              <Step index={1} text="Turn the camera on and enable its Wi-Fi access point." />
              <Step
                index={2}
                text="In the phone's Wi-Fi settings, join the camera's network. It will have no internet — that is expected."
              />
              <Step index={3} text="Come back here and tap Connect." />
            </View>
          </Card>

          <Button
            label={connecting ? 'Looking for a camera…' : 'Connect to camera'}
            variant="primary"
            onPress={() => void connect()}
            loading={connecting}
          />

          {error ? <Callout tone="bad" title="Not connected">{error}</Callout> : null}

          <Card>
            <Label>Addresses tried automatically</Label>
            {KNOWN_CAMERA_HOSTS.map((candidate) => (
              <View key={candidate.host} style={styles.hostRow}>
                <Text style={styles.hostAddress}>{candidate.host.replace('http://', '')}</Text>
                <Text style={styles.hostHint}>{candidate.hint}</Text>
              </View>
            ))}
            <Divider />
            <Field label="Or enter the camera's address">
              <Input
                value={manualHost}
                onChangeText={setManualHost}
                placeholder="192.168.1.1"
                autoCapitalize="none"
                autoCorrect={false}
                keyboardType="numbers-and-punctuation"
              />
            </Field>
            <Button
              label="Connect to this address"
              onPress={() => void connect(manualHost)}
              loading={connecting}
              disabled={manualHost.trim().length === 0}
            />
          </Card>

          <Callout tone="info" title="Which cameras work">
            Any camera implementing the Open Spherical Camera standard, which is an open published
            specification rather than a vendor SDK. The protocol is tested; it has not been run
            against physical hardware on this project, so treat the first connection to a new model
            as a test.
          </Callout>
        </>
      ) : (
        <>
          <Card>
            <View style={styles.headerRow}>
              <View style={styles.grow}>
                <Text style={styles.model}>{connection.info.model}</Text>
                <Text style={styles.manufacturer}>
                  {connection.info.manufacturer}
                  {connection.info.firmwareVersion ? ` · ${connection.info.firmwareVersion}` : ''}
                </Text>
              </View>
              <Badge label={recording ? 'recording' : 'ready'} tone={recording ? 'warn' : 'good'} />
            </View>

            <Divider />

            <Detail
              label="Battery"
              value={batteryPercent === null ? 'not reported' : `${batteryPercent}%`}
              tone={lowBattery ? 'bad' : undefined}
            />
            <Detail
              label="Recording time left"
              value={formatSeconds(connection.state.remainingVideoSeconds)}
            />
            <Detail
              label="Storage left"
              value={formatBytes(connection.state.storageBytesRemaining)}
            />
            <Detail label="Address" value={connection.client.baseUrl.replace('http://', '')} />
            {connection.info.serialNumber ? (
              <Detail label="Serial" value={connection.info.serialNumber} />
            ) : null}
          </Card>

          {lowBattery ? (
            <Callout tone="bad" title={`Battery at ${batteryPercent}%`}>
              A camera that dies mid-walkthrough loses the recording it was still writing. Charge
              or swap the battery before starting a long capture.
            </Callout>
          ) : null}

          {connection.state.remainingVideoSeconds !== null &&
          connection.state.remainingVideoSeconds < 300 ? (
            <Callout tone="warn" title="Little recording time left">
              The camera reports under five minutes of space. Clear files off it before capturing a
              full floor.
            </Callout>
          ) : null}

          {!connection.info.advertisesVideo ? (
            <Callout tone="info" title="Video support not advertised">
              This camera does not declare OSC API level 2, so the app cannot tell in advance
              whether it records video. Starting a capture will try anyway, and the camera's own
              response decides — it is not a prediction either way.
            </Callout>
          ) : null}

          <Button label="Go to capture" variant="primary" onPress={onReady} />
          <Button label="Disconnect" variant="ghost" onPress={disconnect} />
        </>
      )}
    </ScrollView>
  )
}

function Step({ index, text }: { index: number; text: string }) {
  return (
    <View style={styles.step}>
      <View style={styles.stepNumber}>
        <Text style={styles.stepNumberText}>{index}</Text>
      </View>
      <Text style={styles.stepText}>{text}</Text>
    </View>
  )
}

function Detail({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone?: 'bad'
}) {
  return (
    <View style={styles.detailRow}>
      <Text style={styles.detailLabel}>{label}</Text>
      <Text style={[styles.detailValue, tone === 'bad' ? { color: theme.bad } : null]}>{value}</Text>
    </View>
  )
}

function formatSeconds(seconds: number | null): string {
  if (seconds === null) return 'not reported'
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} min`
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`
}

function formatBytes(bytes: number | null): string {
  if (bytes === null) return 'not reported'
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${value.toFixed(value < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`
}

const styles = StyleSheet.create({
  content: { padding: spacing.lg, gap: spacing.md, paddingBottom: spacing.xl * 2 },
  steps: { gap: spacing.md, marginTop: spacing.sm },
  step: { flexDirection: 'row', gap: spacing.md, alignItems: 'flex-start' },
  stepNumber: {
    width: 24,
    height: 24,
    borderRadius: 12,
    borderWidth: 1,
    borderColor: theme.brand,
    alignItems: 'center',
    justifyContent: 'center',
  },
  stepNumberText: { color: theme.brand, fontSize: 12, fontWeight: '700' },
  stepText: { color: theme.ink, flex: 1, lineHeight: 20 },
  hostRow: { marginTop: spacing.sm },
  hostAddress: { color: theme.ink, fontVariant: ['tabular-nums'], fontSize: 15 },
  hostHint: { color: theme.inkSubtle, fontSize: 12 },
  headerRow: { flexDirection: 'row', alignItems: 'center', gap: spacing.md },
  grow: { flex: 1 },
  model: { color: theme.ink, fontSize: 17, fontWeight: '700' },
  manufacturer: { color: theme.inkMuted, fontSize: 13 },
  detailRow: {
    flexDirection: 'row',
    justifyContent: 'space-between',
    paddingVertical: spacing.xs,
  },
  detailLabel: { color: theme.inkMuted },
  detailValue: { color: theme.ink, fontVariant: ['tabular-nums'] },
})
