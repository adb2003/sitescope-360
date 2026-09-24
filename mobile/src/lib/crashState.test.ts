/**
 * What the crash screen decides.
 *
 * A boundary on this app is not a nicety. A render error in a release build takes React Native
 * down to the home screen, so the operator on a half-built floor loses the tool mid-capture and
 * has no way to learn that their recordings are still on the device — they are, in SQLite and in
 * the app's own files, which is why the fallback leads with that rather than with the error.
 *
 * The decisions below are each wrong in a way a screenshot would not show: a boundary that never
 * resets follows the operator to every other tab, one that resets constantly flickers straight
 * back into the crash, and copy that tells someone to switch tabs when the whole app failed is
 * advice to do something impossible.
 */

import { describe, expect, it } from 'vitest'

import { describeCrash, describeError, shouldReset } from './crashState'

describe('shouldReset', () => {
  it('clears when the key changes, so switching tabs recovers', () => {
    expect(shouldReset(true, 'capture', 'queue')).toBe(true)
  })

  it('stays put while the key is unchanged', () => {
    // Re-rendering the same tab must not clear a real failure: that flickers between the
    // fallback and the crash rather than settling on either.
    expect(shouldReset(true, 'capture', 'capture')).toBe(false)
  })

  it('does nothing when there is no error to clear', () => {
    expect(shouldReset(false, 'capture', 'queue')).toBe(false)
  })

  it('treats an absent key as a key', () => {
    // The root boundary passes none. Going from undefined to a value is still a change, and
    // pretending otherwise would leave the outer fallback stuck for the life of the process.
    expect(shouldReset(true, undefined, 'capture')).toBe(true)
    expect(shouldReset(true, undefined, undefined)).toBe(false)
  })
})

describe('describeCrash', () => {
  it('leads with the reassurance, not the error', () => {
    // The question an operator actually has after a screen vanishes is whether the walk they
    // just recorded is gone.
    const copy = describeCrash(new Error('boom'))

    expect(copy.reassurance).toMatch(/captures are safe/i)
    expect(copy.reassurance).toMatch(/still stored here/i)
  })

  it('tells a screen crash to switch tabs and an app crash to reopen', () => {
    // There is nothing to switch to when the shell itself failed, so advising it would be
    // advice to do something impossible.
    expect(describeCrash(new Error('x'), 'screen').advice).toMatch(/another tab/i)
    expect(describeCrash(new Error('x'), 'app').advice).toMatch(/close and reopen/i)
  })

  it('names the scope in the title', () => {
    expect(describeCrash(new Error('x'), 'screen').title).toMatch(/this screen/i)
    expect(describeCrash(new Error('x'), 'app').title).toMatch(/could not start/i)
  })

  it('defaults to the screen scope', () => {
    expect(describeCrash(new Error('x')).title).toBe(describeCrash(new Error('x'), 'screen').title)
  })
})

describe('describeError', () => {
  it('names the error and its message', () => {
    expect(describeError(new TypeError('undefined is not an object'))).toBe(
      'TypeError: undefined is not an object',
    )
  })

  it('never returns an empty string', () => {
    // `throw` accepts anything, and a detail box rendering "" tells the operator strictly less
    // than one admitting it has nothing to show.
    expect(describeError(new Error(''))).toBe('Error (no message)')
    expect(describeError(new Error('   '))).toBe('Error (no message)')
    expect(describeError(undefined)).toMatch(/no description/i)
    expect(describeError(null)).toMatch(/no description/i)
    expect(describeError({})).toMatch(/no description/i)
    expect(describeError('   ')).toMatch(/no description/i)
  })

  it('passes a thrown string through', () => {
    // Older libraries still `throw 'something went wrong'`, and that string is more useful than
    // a generic placeholder.
    expect(describeError('websocket closed')).toBe('websocket closed')
  })
})
