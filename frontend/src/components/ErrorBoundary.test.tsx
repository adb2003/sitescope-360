/**
 * The boundary between a render error and a blank page.
 *
 * Every assertion here is about what the operator is left holding. Without a boundary React
 * unmounts the whole tree: white screen, no message, no way back, and no indication that the
 * capture they queued three minutes ago is safe on the device. It is — in IndexedDB — but a
 * blank page says nothing, and the reasonable assumption on a building site is that the walk was
 * lost.
 *
 * The reset behaviour matters as much as the catch. A boundary that keeps showing its fallback
 * after the operator navigates elsewhere has replaced one broken page with a broken application.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ErrorBoundary } from './ErrorBoundary'

function Boom({ message = 'kaboom' }: { message?: string }): JSX.Element {
  throw new Error(message)
}

beforeEach(() => {
  // React logs caught render errors to the console by design, and the boundary logs the
  // component stack on purpose. Silenced so a passing run is not full of red.
  vi.spyOn(console, 'error').mockImplementation(() => undefined)
})

afterEach(() => vi.restoreAllMocks())

describe('ErrorBoundary', () => {
  it('renders its children when nothing throws', () => {
    render(
      <ErrorBoundary>
        <p>The capture page</p>
      </ErrorBoundary>,
    )

    expect(screen.getByText('The capture page')).toBeInTheDocument()
  })

  it('shows a fallback instead of unmounting when a child throws', () => {
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )

    expect(screen.getByRole('alert')).toBeInTheDocument()
    expect(screen.getByText(/this page stopped working/i)).toBeInTheDocument()
  })

  it('tells the operator their queued captures are safe', () => {
    // The question somebody on a floor with no signal actually has, and the one a blank screen
    // answers worst. Queued uploads live in IndexedDB and survive this.
    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )

    expect(screen.getByText(/nothing you have captured is lost/i)).toBeInTheDocument()
  })

  it('shows the error so a report from the field is actionable', () => {
    render(
      <ErrorBoundary>
        <Boom message="cannot read properties of undefined" />
      </ErrorBoundary>,
    )

    expect(screen.getByText('Error')).toBeInTheDocument()
    expect(screen.getByText(/cannot read properties of undefined/)).toBeInTheDocument()
  })

  it('says so rather than showing nothing when the error carries no message', () => {
    render(
      <ErrorBoundary>
        <Boom message="" />
      </ErrorBoundary>,
    )

    expect(screen.getByText(/no message was attached/i)).toBeInTheDocument()
  })

  it('logs the component stack, which exists nowhere else', () => {
    // React does not attach it to the error, so if the boundary does not log it, the one piece
    // of information saying *where* the crash happened is gone.
    const logged = vi.spyOn(console, 'error')

    render(
      <ErrorBoundary>
        <Boom />
      </ErrorBoundary>,
    )

    expect(
      logged.mock.calls.some((call) => String(call[0]).includes('Unhandled render error')),
    ).toBe(true)
  })

  it('uses different wording when the shell itself failed', () => {
    // There is no navigation left in that case, so the copy must not suggest going back to
    // something that is not there.
    render(
      <ErrorBoundary scope="app">
        <Boom />
      </ErrorBoundary>,
    )

    expect(screen.getByText(/could not start this screen/i)).toBeInTheDocument()
    expect(screen.getByText(/reloading usually clears it/i)).toBeInTheDocument()
  })

  it('recovers when the reset key changes, so navigating away clears it', () => {
    // Without this the fallback follows the operator to every other page until they reload —
    // the boundary becomes a worse bug than the one it caught.
    const { rerender } = render(
      <ErrorBoundary resetKey="/viewer">
        <Boom />
      </ErrorBoundary>,
    )
    expect(screen.getByRole('alert')).toBeInTheDocument()

    rerender(
      <ErrorBoundary resetKey="/capture">
        <p>The capture page</p>
      </ErrorBoundary>,
    )

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByText('The capture page')).toBeInTheDocument()
  })

  it('stays broken while the reset key is unchanged', () => {
    // Re-rendering the same route must not silently clear a real, ongoing failure: that would
    // flicker between the fallback and a crash rather than settling on either.
    const { rerender } = render(
      <ErrorBoundary resetKey="/viewer">
        <Boom />
      </ErrorBoundary>,
    )

    rerender(
      <ErrorBoundary resetKey="/viewer">
        <p>Recovered somehow</p>
      </ErrorBoundary>,
    )

    expect(screen.getByRole('alert')).toBeInTheDocument()
  })

  it('retries on demand', async () => {
    let shouldThrow = true
    function Flaky(): JSX.Element {
      if (shouldThrow) throw new Error('transient')
      return <p>Loaded on the second attempt</p>
    }

    render(
      <ErrorBoundary>
        <Flaky />
      </ErrorBoundary>,
    )
    expect(screen.getByRole('alert')).toBeInTheDocument()

    shouldThrow = false
    await userEvent.click(screen.getByRole('button', { name: /try again/i }))

    expect(screen.getByText('Loaded on the second attempt')).toBeInTheDocument()
  })
})
