import { useState } from 'react'
import { KeyboardAvoidingView, Platform, ScrollView, StyleSheet, Text, View } from 'react-native'

import { Body, Button, Callout, Card, Field, Heading, Input } from '../components/ui'
import { API_BASE_URL, ApiError, api } from '../lib/api'
import { spacing, theme } from '../theme'

export function SignInScreen({ onSignedIn }: { onSignedIn: () => void }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function submit() {
    setError(null)
    setBusy(true)
    try {
      await api.login(email.trim(), password)
      onSignedIn()
    } catch (caught) {
      setError(
        caught instanceof ApiError
          ? caught.message
          : `Cannot reach the server at ${API_BASE_URL}. Check the address and that you are on the same network.`,
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <KeyboardAvoidingView
      style={styles.flex}
      behavior={Platform.OS === 'ios' ? 'padding' : undefined}
    >
      <ScrollView contentContainerStyle={styles.content} keyboardShouldPersistTaps="handled">
        <View style={styles.brand}>
          <View style={styles.mark} />
          <View>
            <Text style={styles.title}>SiteScope 360</Text>
            <Text style={styles.subtitle}>Site capture</Text>
          </View>
        </View>

        <Card>
          <Heading>Sign in</Heading>
          <Field label="Email">
            <Input
              value={email}
              onChangeText={setEmail}
              autoCapitalize="none"
              autoCorrect={false}
              keyboardType="email-address"
              textContentType="username"
              placeholder="you@example.com"
            />
          </Field>
          <Field label="Password">
            <Input
              value={password}
              onChangeText={setPassword}
              secureTextEntry
              textContentType="password"
              onSubmitEditing={submit}
              returnKeyType="go"
            />
          </Field>

          {error ? <Callout tone="bad">{error}</Callout> : null}

          <Button label="Sign in" variant="primary" onPress={submit} loading={busy} />
        </Card>

        <Callout tone="info" title="Works offline">
          You can start a capture, record and finish it with no signal. Sessions and files are held
          on the device and upload themselves when the connection returns — but you must sign in at
          least once first.
        </Callout>

        <Body muted>Server: {API_BASE_URL}</Body>
      </ScrollView>
    </KeyboardAvoidingView>
  )
}

const styles = StyleSheet.create({
  flex: { flex: 1, backgroundColor: theme.canvas },
  content: { padding: spacing.lg, gap: spacing.lg, justifyContent: 'center', flexGrow: 1 },
  brand: { flexDirection: 'row', alignItems: 'center', gap: spacing.md },
  mark: { width: 34, height: 34, borderRadius: 8, backgroundColor: theme.brand },
  title: { color: theme.ink, fontSize: 22, fontWeight: '700' },
  subtitle: { color: theme.inkSubtle, fontSize: 12, textTransform: 'uppercase', letterSpacing: 1 },
})
