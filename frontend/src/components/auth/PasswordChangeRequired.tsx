/**
 * The screen an account sees while an administrator's password reset is outstanding.
 *
 * It replaces the application entirely rather than appearing as a banner over it. That is not a
 * stylistic choice: the server holds such an account to `/auth/me` and `/auth/change-password`
 * and refuses everything else with a 403, so a shell rendered around this would be a grid of
 * panels each reporting its own error, and the one thing the person needs to do would be the
 * hardest thing on screen to find.
 *
 * Signing out is offered because the alternative is a trap. Somebody who opens this on a shared
 * machine, or who was handed the wrong temporary password, has to be able to leave.
 */

import { KeyRound } from 'lucide-react'
import { useState, type FormEvent } from 'react'

import { Button, Callout, Card, Field, Input } from '@/components/ui'
import { useChangeOwnPassword } from '@/hooks/queries'
import { useAuth } from '@/hooks/useAuth'
import { ApiError } from '@/lib/api'

export function PasswordChangeRequired({ email }: { email: string }) {
  const change = useChangeOwnPassword()
  const { signOut } = useAuth()
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)

  const submit = (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    if (next !== confirm) {
      // Checked here as well as by the server having no idea: a typo in a password nobody can
      // read back is the one mistake this form exists to catch.
      setError('The two new passwords do not match')
      return
    }
    change.mutate(
      { current_password: current, new_password: next },
      {
        onError: (caught) =>
          setError(caught instanceof ApiError ? caught.message : 'Could not set the password'),
      },
    )
  }

  return (
    <div className="grid min-h-screen place-items-center bg-surface-sunken p-4">
      <Card className="w-full max-w-md p-6">
        <div className="mb-4 flex items-start gap-3">
          <span className="grid size-9 shrink-0 place-items-center rounded-full bg-warn/12 text-warn">
            <KeyRound className="size-4" aria-hidden />
          </span>
          <div>
            <h1 className="text-base font-semibold text-ink">Set a new password</h1>
            <p className="text-xs text-ink-muted">
              An administrator reset the password for {email}. That password is a secret somebody
              else chose, so it cannot stay on this account — choose your own to continue.
            </p>
          </div>
        </div>

        {error ? (
          <Callout tone="bad" className="mb-3">
            {error}
          </Callout>
        ) : null}

        <form onSubmit={submit} className="space-y-3">
          <Field label="The password you were given">
            <Input
              type="password"
              autoComplete="current-password"
              value={current}
              onChange={(event) => setCurrent(event.target.value)}
              required
            />
          </Field>
          <Field label="New password" hint="At least 10 characters, and not the one above">
            <Input
              type="password"
              autoComplete="new-password"
              minLength={10}
              value={next}
              onChange={(event) => setNext(event.target.value)}
              required
            />
          </Field>
          <Field label="New password again">
            <Input
              type="password"
              autoComplete="new-password"
              minLength={10}
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              required
            />
          </Field>
          <div className="flex items-center gap-2 pt-1">
            <Button type="submit" variant="primary" loading={change.isPending}>
              Set password
            </Button>
            <Button type="button" variant="ghost" onClick={() => void signOut()}>
              Sign out instead
            </Button>
          </div>
        </form>
      </Card>
    </div>
  )
}
