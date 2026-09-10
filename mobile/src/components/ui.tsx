/** Small set of shared primitives, sized for gloved one-handed use on site. */

import type { ReactNode } from 'react'
import {
  ActivityIndicator,
  Pressable,
  StyleSheet,
  Text,
  TextInput,
  View,
  type TextInputProps,
  type ViewStyle,
} from 'react-native'

import { TOUCH_TARGET, spacing, theme } from '../theme'

export function Button({
  label,
  onPress,
  variant = 'secondary',
  loading,
  disabled,
  style,
}: {
  label: string
  onPress: () => void
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost'
  loading?: boolean
  disabled?: boolean
  style?: ViewStyle
}) {
  const palette = {
    primary: { bg: theme.brand, fg: theme.brandInk, border: theme.brand },
    secondary: { bg: theme.surfaceRaised, fg: theme.ink, border: theme.lineStrong },
    danger: { bg: theme.bad, fg: '#ffffff', border: theme.bad },
    ghost: { bg: 'transparent', fg: theme.inkMuted, border: 'transparent' },
  }[variant]

  const inactive = disabled || loading

  return (
    <Pressable
      onPress={onPress}
      disabled={inactive}
      accessibilityRole="button"
      accessibilityLabel={label}
      accessibilityState={{ disabled: Boolean(inactive), busy: Boolean(loading) }}
      style={({ pressed }) => [
        styles.button,
        {
          backgroundColor: palette.bg,
          borderColor: palette.border,
          opacity: inactive ? 0.45 : pressed ? 0.8 : 1,
        },
        style,
      ]}
    >
      {loading ? <ActivityIndicator size="small" color={palette.fg} /> : null}
      <Text style={[styles.buttonLabel, { color: palette.fg }]}>{label}</Text>
    </Pressable>
  )
}

export function Card({ children, style }: { children: ReactNode; style?: ViewStyle }) {
  return <View style={[styles.card, style]}>{children}</View>
}

export function Label({ children }: { children: ReactNode }) {
  return <Text style={styles.label}>{children}</Text>
}

export function Body({ children, muted }: { children: ReactNode; muted?: boolean }) {
  return <Text style={[styles.body, muted && { color: theme.inkMuted }]}>{children}</Text>
}

export function Heading({ children }: { children: ReactNode }) {
  return <Text style={styles.heading}>{children}</Text>
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: ReactNode
}) {
  return (
    <View style={{ marginBottom: spacing.md }}>
      <Label>{label}</Label>
      {children}
      {hint ? <Text style={styles.hint}>{hint}</Text> : null}
    </View>
  )
}

export function Input(props: TextInputProps) {
  return (
    <TextInput
      placeholderTextColor={theme.inkSubtle}
      {...props}
      style={[styles.input, props.style]}
    />
  )
}

export function Badge({
  label,
  tone = 'neutral',
}: {
  label: string
  tone?: 'neutral' | 'good' | 'warn' | 'bad' | 'brand' | 'estimated'
}) {
  const colour = {
    neutral: theme.inkMuted,
    good: theme.good,
    warn: theme.warn,
    bad: theme.bad,
    brand: theme.brand,
    estimated: theme.estimated,
  }[tone]

  return (
    <View style={[styles.badge, { borderColor: colour }]}>
      <Text style={[styles.badgeText, { color: colour }]}>{label.toUpperCase()}</Text>
    </View>
  )
}

/** Used wherever the app must state a limitation rather than imply confidence (§46). */
export function Callout({
  tone = 'info',
  title,
  children,
}: {
  tone?: 'info' | 'warn' | 'bad' | 'good'
  title?: string
  children?: ReactNode
}) {
  const colour = { info: theme.info, warn: theme.warn, bad: theme.bad, good: theme.good }[tone]
  return (
    <View style={[styles.callout, { borderColor: colour }]}>
      {title ? <Text style={[styles.calloutTitle, { color: colour }]}>{title}</Text> : null}
      {children ? <Text style={styles.calloutBody}>{children}</Text> : null}
    </View>
  )
}

export function Divider() {
  return <View style={styles.divider} />
}

const styles = StyleSheet.create({
  button: {
    minHeight: TOUCH_TARGET,
    flexDirection: 'row',
    alignItems: 'center',
    justifyContent: 'center',
    gap: spacing.sm,
    paddingHorizontal: spacing.lg,
    borderRadius: 6,
    borderWidth: 1,
  },
  buttonLabel: { fontSize: 16, fontWeight: '600' },
  card: {
    backgroundColor: theme.surface,
    borderColor: theme.line,
    borderWidth: 1,
    borderRadius: 9,
    padding: spacing.lg,
  },
  label: {
    color: theme.inkSubtle,
    fontSize: 11,
    fontWeight: '600',
    letterSpacing: 0.6,
    textTransform: 'uppercase',
    marginBottom: spacing.xs,
  },
  body: { color: theme.ink, fontSize: 15, lineHeight: 21 },
  heading: { color: theme.ink, fontSize: 20, fontWeight: '700', marginBottom: spacing.sm },
  hint: { color: theme.inkSubtle, fontSize: 12, marginTop: spacing.xs },
  input: {
    minHeight: TOUCH_TARGET,
    backgroundColor: theme.surfaceRaised,
    borderColor: theme.lineStrong,
    borderWidth: 1,
    borderRadius: 6,
    paddingHorizontal: spacing.md,
    color: theme.ink,
    fontSize: 16,
  },
  badge: {
    borderWidth: 1,
    borderRadius: 4,
    paddingHorizontal: 6,
    paddingVertical: 2,
    alignSelf: 'flex-start',
  },
  badgeText: { fontSize: 10, fontWeight: '700', letterSpacing: 0.5 },
  callout: {
    borderLeftWidth: 3,
    borderRadius: 4,
    backgroundColor: theme.surfaceRaised,
    padding: spacing.md,
    marginVertical: spacing.sm,
  },
  calloutTitle: { fontSize: 13, fontWeight: '700', marginBottom: 2 },
  calloutBody: { color: theme.inkMuted, fontSize: 13, lineHeight: 19 },
  divider: { height: 1, backgroundColor: theme.line, marginVertical: spacing.md },
})
