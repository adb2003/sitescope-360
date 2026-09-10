import { CircleDot, ShieldCheck } from 'lucide-react'
import { useEffect, useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router-dom'

import { Button, Callout, Card, Field, Input } from '@/components/ui'
import { useAuth } from '@/hooks/useAuth'

export function LoginPage() {
  const { session, signIn, error } = useAuth()
  const navigate = useNavigate()
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [submitting, setSubmitting] = useState(false)

  useEffect(() => {
    if (session) navigate('/', { replace: true })
  }, [session, navigate])

  async function onSubmit(event: FormEvent) {
    event.preventDefault()
    setSubmitting(true)
    try {
      await signIn(email.trim(), password)
      navigate('/', { replace: true })
    } catch {
      // The error is surfaced from the auth context below.
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-canvas p-4">
      <div className="w-full max-w-sm">
        <div className="mb-5 flex items-center gap-2.5">
          <div className="flex size-9 items-center justify-center rounded-lg bg-brand text-brand-ink">
            <CircleDot className="size-5" aria-hidden />
          </div>
          <div>
            <h1 className="text-xl font-semibold tracking-tight text-ink">SiteScope 360</h1>
            <p className="text-xs text-ink-muted">Construction 360° site monitoring</p>
          </div>
        </div>

        <Card>
          <form onSubmit={onSubmit} className="space-y-3 p-4">
            <Field label="Email" required>
              <Input
                type="email"
                value={email}
                onChange={(event) => setEmail(event.target.value)}
                autoComplete="username"
                placeholder="you@example.com"
                required
                autoFocus
              />
            </Field>

            <Field label="Password" required>
              <Input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                required
              />
            </Field>

            {error ? <Callout tone="bad">{error}</Callout> : null}

            <Button type="submit" variant="primary" size="lg" className="w-full" loading={submitting}>
              Sign in
            </Button>
          </form>
        </Card>

        <Callout tone="info" title="Independent platform" className="mt-4">
          SiteScope 360 uses no OpenSpace (or other third-party construction-monitoring vendor)
          API, SDK, application or account. Capture processing, 360 viewing, floor-plan mapping and
          reporting are all implemented in this platform.
        </Callout>

        <p className="mt-4 flex items-start gap-1.5 text-2xs leading-relaxed text-ink-subtle">
          <ShieldCheck className="mt-px size-3.5 shrink-0" aria-hidden />
          <span>
            After seeding demo data (<code className="font-mono">python -m app.seed --demo</code>),
            sign in as <code className="font-mono">admin@demo.example.com</code>. The seeder prints
            the password.
          </span>
        </p>
      </div>
    </div>
  )
}
