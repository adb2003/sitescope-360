/**
 * SiteScope 360 capture app (§5, §32).
 *
 * Three tabs and a sign-in gate, deliberately: connect the camera, run the capture, watch the
 * queue drain. The order is the order of the job. A site engineer needs to start a capture and see
 * what is still waiting to upload, and nothing else. Everything analytical lives in the web
 * dashboard.
 *
 * Navigation is hand-rolled rather than using a router library. With two screens, a router would
 * add a dependency, a native-stack build step and a linking config to solve a problem that is one
 * piece of state.
 */

import * as Network from 'expo-network'
import { StatusBar } from 'expo-status-bar'
import { useCallback, useEffect, useState } from 'react'
import { ActivityIndicator, Pressable, StyleSheet, Text, View } from 'react-native'
import { SafeAreaProvider, SafeAreaView } from 'react-native-safe-area-context'

import { ErrorBoundary } from './src/components/ErrorBoundary'
import { tokens } from './src/lib/api'
import { openQueue, pendingCount, sync, QUEUE_IS_AVAILABLE } from './src/lib/queue'
import { CameraScreen } from './src/screens/CameraScreen'
import { CaptureScreen } from './src/screens/CaptureScreen'
import { QueueScreen } from './src/screens/QueueScreen'
import { SignInScreen } from './src/screens/SignInScreen'
import { spacing, theme } from './src/theme'

type Tab = 'camera' | 'capture' | 'queue'

/**
 * The outer boundary, for a failure during boot or in the shell itself.
 *
 * Separate from the per-tab one because there is nothing to switch to when this fires: opening
 * the local database, restoring the session and the sign-in gate all happen out here, and a
 * throw from any of them would otherwise close the app on launch — the worst possible moment,
 * since the operator cannot even reach the queue to confirm their captures are still there.
 */
export default function App() {
  return (
    <ErrorBoundary scope="app">
      <CaptureApp />
    </ErrorBoundary>
  )
}

function CaptureApp() {
  const [ready, setReady] = useState(false)
  const [signedIn, setSignedIn] = useState(false)
  const [tab, setTab] = useState<Tab>('capture')
  const [online, setOnline] = useState(true)
  const [waiting, setWaiting] = useState(0)

  const refreshQueueCount = useCallback(() => {
    if (!QUEUE_IS_AVAILABLE) return
    void pendingCount().then(setWaiting)
  }, [])

  // ---- boot -------------------------------------------------------------
  useEffect(() => {
    void (async () => {
      // The local queue must exist before anything else: a capture started while offline writes
      // to it immediately. Under Expo web there is no durable queue at all, and the app must
      // still boot -- web exists to inspect the interface on a desktop, and refusing to start
      // would defeat that while telling the operator nothing.
      if (QUEUE_IS_AVAILABLE) {
        await openQueue()
      }
      const { access } = await tokens.get()
      setSignedIn(Boolean(access))
      refreshQueueCount()
      setReady(true)
    })()
  }, [refreshQueueCount])

  // ---- connectivity -----------------------------------------------------
  useEffect(() => {
    let cancelled = false

    const check = async () => {
      const state = await Network.getNetworkStateAsync()
      // `isInternetReachable` is undefined on some platforms; falling back to isConnected is
      // better than reporting offline and refusing to try.
      const reachable = state.isInternetReachable ?? state.isConnected ?? false
      if (cancelled) return

      setOnline((previous) => {
        // A transition from offline to online is the moment to drain the queue (§32).
        if (!previous && reachable && QUEUE_IS_AVAILABLE) {
          void sync().finally(refreshQueueCount)
        }
        return reachable
      })
    }

    void check()
    const timer = setInterval(check, 15000)
    return () => {
      cancelled = true
      clearInterval(timer)
    }
  }, [refreshQueueCount])

  if (!ready) {
    return (
      <SafeAreaProvider>
        <View style={styles.boot}>
          <ActivityIndicator color={theme.brand} />
          <Text style={styles.bootText}>Preparing local storage…</Text>
        </View>
      </SafeAreaProvider>
    )
  }

  if (!signedIn) {
    return (
      <SafeAreaProvider>
        <StatusBar style="light" />
        <SafeAreaView style={styles.flex} edges={['top', 'bottom']}>
          <SignInScreen onSignedIn={() => setSignedIn(true)} />
        </SafeAreaView>
      </SafeAreaProvider>
    )
  }

  return (
    <SafeAreaProvider>
      <StatusBar style="light" />
      <SafeAreaView style={styles.flex} edges={['top', 'bottom']}>
        <View style={styles.header}>
          <View style={styles.mark} />
          <Text style={styles.title}>SiteScope 360</Text>
          <View style={[styles.pill, { borderColor: online ? theme.good : theme.warn }]}>
            <Text style={[styles.pillText, { color: online ? theme.good : theme.warn }]}>
              {online ? 'ONLINE' : 'OFFLINE'}
            </Text>
          </View>
          <Pressable
            onPress={async () => {
              await tokens.clear()
              setSignedIn(false)
            }}
            accessibilityRole="button"
            accessibilityLabel="Sign out"
          >
            <Text style={styles.signOut}>Sign out</Text>
          </Pressable>
        </View>

        {/* Inside the tab bar, so a screen that crashes leaves the other tabs reachable — the
            operator whose capture screen died can still open the queue and see their recordings
            waiting. Keyed on the tab, so switching away clears it without them needing to know
            what a boundary is. */}
        <View style={styles.flex}>
          <ErrorBoundary resetKey={tab}>
            {tab === 'camera' ? (
              <CameraScreen onReady={() => setTab('capture')} />
            ) : tab === 'capture' ? (
              <CaptureScreen
                online={online}
                onQueueChanged={refreshQueueCount}
                onOpenQueue={() => setTab('queue')}
                onOpenCamera={() => setTab('camera')}
              />
            ) : (
              <QueueScreen online={online} onQueueChanged={refreshQueueCount} />
            )}
          </ErrorBoundary>
        </View>

        <View style={styles.tabBar}>
          <TabButton label="Camera" active={tab === 'camera'} onPress={() => setTab('camera')} />
          <TabButton
            label="Capture"
            active={tab === 'capture'}
            onPress={() => setTab('capture')}
          />
          <TabButton
            label="Upload queue"
            active={tab === 'queue'}
            badge={waiting || undefined}
            onPress={() => setTab('queue')}
          />
        </View>
      </SafeAreaView>
    </SafeAreaProvider>
  )
}

function TabButton({
  label,
  active,
  badge,
  onPress,
}: {
  label: string
  active: boolean
  badge?: number
  onPress: () => void
}) {
  return (
    <Pressable
      onPress={onPress}
      accessibilityRole="tab"
      accessibilityState={{ selected: active }}
      style={[styles.tab, active && styles.tabActive]}
    >
      <Text style={[styles.tabText, active && styles.tabTextActive]}>{label}</Text>
      {badge ? (
        <View style={styles.tabBadge}>
          <Text style={styles.tabBadgeText}>{badge}</Text>
        </View>
      ) : null}
    </Pressable>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1, backgroundColor: theme.canvas },
  boot: {
    flex: 1,
    alignItems: 'center',
    justifyContent: 'center',
    gap: spacing.md,
    backgroundColor: theme.canvas,
  },
  bootText: { color: theme.inkMuted, fontSize: 14 },
  header: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: spacing.sm,
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.md,
    borderBottomWidth: 1,
    borderBottomColor: theme.line,
    backgroundColor: theme.surface,
  },
  mark: { width: 22, height: 22, borderRadius: 5, backgroundColor: theme.brand },
  title: { color: theme.ink, fontSize: 16, fontWeight: '700', flex: 1 },
  pill: { borderWidth: 1, borderRadius: 4, paddingHorizontal: 6, paddingVertical: 2 },
  pillText: { fontSize: 10, fontWeight: '700', letterSpacing: 0.5 },
  signOut: { color: theme.inkMuted, fontSize: 13 },
  tabBar: {
    flexDirection: 'row',
    borderTopWidth: 1,
    borderTopColor: theme.line,
    backgroundColor: theme.surface,
  },
  tab: {
    flex: 1,
    minHeight: 56,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: spacing.sm,
  },
  tabActive: { borderTopWidth: 2, borderTopColor: theme.brand },
  tabText: { color: theme.inkMuted, fontSize: 14, fontWeight: '600' },
  tabTextActive: { color: theme.brand },
  tabBadge: {
    minWidth: 20,
    height: 20,
    borderRadius: 10,
    paddingHorizontal: 5,
    alignItems: 'center',
    justifyContent: 'center',
    backgroundColor: theme.warn,
  },
  tabBadgeText: { color: '#1a1204', fontSize: 11, fontWeight: '700' },
})
