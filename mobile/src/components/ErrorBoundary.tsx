/**
 * The last thing between a render error and the app closing (§32).
 *
 * On React Native this matters more than it does in a browser. A thrown render error in a release
 * build does not leave a blank page to read — it takes the app down to the home screen. The
 * operator is on a half-built floor with no signal, and the tool they came to record with has
 * just disappeared; the obvious next move is to relaunch straight back into the same crash.
 *
 * Their work is not lost. Sessions and the queue are in SQLite, the recordings are files in the
 * app's own directory, and all of it survives a crash and a relaunch. Nothing on a vanished
 * screen says so, which is why the fallback leads with that rather than with the error.
 *
 * Two of these, as on the web: one inside the tab bar so a screen that crashes leaves the other
 * tabs reachable — the operator can open the queue and *see* their captures still waiting — and
 * one around everything for a failure during boot, where there is nothing left to switch to.
 *
 * **What this does not cover, deliberately.** A boundary sees render errors only. An error thrown
 * from a timer, a native module or an unawaited promise never reaches it, and catching those
 * needs `ErrorUtils.setGlobalHandler`. That is not wired up here: in a release build
 * `console.error` goes nowhere a developer can read, so it would amount to a handler that looks
 * like crash reporting and reports nothing, and doing it properly means a crash-reporting service
 * this project's constraints rule out. Better to be one honest boundary than two, one of which is
 * decorative.
 *
 * A class component because that is still the only way to catch a render error in React.
 */

import { Component, type ErrorInfo, type ReactNode } from 'react'
import { ScrollView, StyleSheet, View } from 'react-native'

import { describeCrash, shouldReset } from '../lib/crashState'
import { spacing, theme } from '../theme'
import { Body, Button, Card, Heading, Label } from './ui'

interface Props {
  children: ReactNode
  /**
   * Changing this clears the fallback.
   *
   * The tab bar passes the active tab, so switching away from a crashed screen recovers without
   * the operator having to understand what happened.
   */
  resetKey?: string
  scope?: 'screen' | 'app'
}

interface State {
  error: unknown
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: unknown): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // The component stack says *where* this happened and exists nowhere else — React does not
    // attach it to the error. In development it is the difference between a useful red box and a
    // shrug; in a release build it is at least present in a device log pulled over USB.
    console.error('Unhandled render error', error, info.componentStack)
  }

  componentDidUpdate(previous: Props): void {
    if (shouldReset(this.state.error !== null, previous.resetKey, this.props.resetKey)) {
      this.setState({ error: null })
    }
  }

  private retry = () => this.setState({ error: null })

  render(): ReactNode {
    const { error } = this.state
    if (error === null) return this.props.children

    const copy = describeCrash(error, this.props.scope ?? 'screen')

    return (
      <ScrollView contentContainerStyle={styles.container}>
        <Card>
          <Heading>{copy.title}</Heading>
          <View style={styles.gap}>
            <Body>{copy.reassurance}</Body>
          </View>
          <View style={styles.gap}>
            <Body muted>{copy.advice}</Body>
          </View>
          <View style={styles.detail}>
            <Label>Error</Label>
            <Body muted>{copy.detail}</Body>
          </View>
          <View style={styles.gap}>
            <Button label="Try again" onPress={this.retry} />
          </View>
        </Card>
      </ScrollView>
    )
  }
}

const styles = StyleSheet.create({
  container: {
    flexGrow: 1,
    justifyContent: 'center',
    padding: spacing.lg,
  },
  gap: {
    marginTop: spacing.md,
  },
  detail: {
    marginTop: spacing.md,
    padding: spacing.sm,
    borderRadius: 6,
    borderWidth: 1,
    borderColor: theme.line,
    backgroundColor: theme.canvas,
  },
})
