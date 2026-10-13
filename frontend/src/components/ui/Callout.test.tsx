/**
 * An error that is not announced is an error nobody heard.
 *
 * Every failure in this application is reported by a `bad` callout — an upload the server
 * refused, a role that could not be changed, a capture that would not process. Each one appears
 * next to the control that failed, which works if you are looking at it. To a screen reader
 * they did not appear at all: the text was inserted into the page and nothing said so, so the
 * operator pressed the button, heard nothing, and pressed it again.
 *
 * The quieter tones stay silent deliberately, and that is the half worth a test of its own: most
 * of them are standing explanations — what the platform does not claim, why a position is
 * estimated — and announcing those on every render is how people learn to ignore the ones that
 * matter.
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { Callout } from './index'

describe('Callout', () => {
  it('announces a failure', () => {
    render(<Callout tone="bad">That file type is not accepted</Callout>)

    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('That file type is not accepted')
  })

  it.each(['info', 'warn', 'estimated'] as const)('leaves a %s callout silent', (tone) => {
    render(<Callout tone={tone}>Positions are estimated, not surveyed</Callout>)

    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    // Still on screen — silent to a screen reader on insertion, not hidden from one.
    expect(screen.getByText('Positions are estimated, not surveyed')).toBeInTheDocument()
  })

  it('keeps the title inside the announced region', () => {
    // Split across two elements in the markup; an alert that announced only the body would read
    // "could not be changed" with no subject.
    render(
      <Callout tone="bad" title="Could not change their role">
        Your role on this project (viewer) cannot perform this action
      </Callout>,
    )

    const alert = screen.getByRole('alert')
    expect(alert).toHaveTextContent('Could not change their role')
    expect(alert).toHaveTextContent('cannot perform this action')
  })
})
