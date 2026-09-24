/**
 * The last line between a render error and a blank page.
 *
 * Without one, React unmounts the whole tree when any component throws: the operator gets white,
 * with no message, no way back, and — the part that matters most here — no idea whether the
 * capture they just queued survived. It did; it is in IndexedDB. Nothing on a blank page says so.
 *
 * Two of these are mounted, and the split is the point:
 *
 * - one around the routed page, inside the shell, so a crash in the viewer leaves the navigation
 *   usable and the operator can walk to the upload queue and see their work is still there;
 * - one around everything, for a failure in the shell itself, where there is nothing left to
 *   navigate with and reloading is the only move.
 *
 * A class component because that is still the only way to catch a render error in React — hooks
 * cannot express `componentDidCatch`.
 */

import { AlertTriangle } from 'lucide-react'
import { Component, type ErrorInfo, type ReactNode } from 'react'

import { Button } from '@/components/ui'
import { getLastCorrelationId } from '@/lib/api'

interface Props {
  children: ReactNode
  /**
   * Changing this resets the boundary.
   *
   * Routes pass their pathname, so navigating away from a page that crashed clears it
   * automatically. Without that the fallback would follow the operator around the application
   * until they reloaded — the boundary would become the bug.
   */
  resetKey?: string
  /** Shown instead of the default copy when the shell itself is what failed. */
  scope?: 'page' | 'app'
}

interface State {
  error: Error | null
  correlationId: string | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, correlationId: null }

  static getDerivedStateFromError(error: Error): Partial<State> {
    // Captured here rather than in `componentDidCatch` so the id belongs to the moment of the
    // failure, not to whatever request happened to finish while the fallback was rendering.
    return { error, correlationId: getLastCorrelationId() }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // The component stack is the only thing that says *where* this happened, and it exists
    // nowhere else — React does not attach it to the error. Logging it is what makes a report
    // from the field actionable.
    console.error('Unhandled render error', error, info.componentStack)
  }

  componentDidUpdate(previous: Props): void {
    if (this.state.error && previous.resetKey !== this.props.resetKey) {
      this.setState({ error: null, correlationId: null })
    }
  }

  private reset = () => this.setState({ error: null, correlationId: null })

  render(): ReactNode {
    const { error, correlationId } = this.state
    if (!error) return this.props.children

    const isApp = this.props.scope === 'app'

    return (
      <div className="flex min-h-[60vh] items-center justify-center p-6" role="alert">
        <div className="panel max-w-lg space-y-3 p-6">
          <div className="flex items-center gap-2">
            <AlertTriangle className="size-5 shrink-0 text-bad" aria-hidden />
            <h1 className="text-lg font-medium text-ink">
              {isApp ? 'SiteScope could not start this screen' : 'This page stopped working'}
            </h1>
          </div>

          {/* The reassurance first, because it is the question an operator on site actually has.
              Queued captures live in IndexedDB and survive a crash, a reload and a closed tab —
              a blank screen tells them none of that, and the fear is that the walk was lost. */}
          <p className="text-sm text-ink-muted">
            Nothing you have captured is lost. Uploads that were queued on this device are still
            queued, and will finish when the connection allows.
          </p>

          <p className="text-sm text-ink-muted">
            {isApp
              ? 'Reloading usually clears it. If it happens again, the details below help us find it.'
              : 'You can go back to what you were doing, or try this page again.'}
          </p>

          <div className="rounded border border-line bg-canvas px-3 py-2">
            <p className="text-xs font-medium text-ink">{error.name}</p>
            <p className="tnum mt-0.5 break-words text-xs text-ink-subtle">
              {error.message || 'No message was attached to the error.'}
            </p>
            {correlationId ? (
              // Labelled as the last request rather than the cause: a render error may have
              // nothing to do with it, and presenting a guess as a fact is how a support thread
              // goes looking in the wrong place for an hour.
              <p className="tnum mt-1 text-xs text-ink-subtle">
                Last server request: {correlationId}
              </p>
            ) : null}
          </div>

          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="primary" onClick={this.reset}>
              Try again
            </Button>
            <Button size="sm" variant="secondary" onClick={() => window.location.reload()}>
              Reload
            </Button>
          </div>
        </div>
      </div>
    )
  }
}
