/**
 * What a crash screen should say, and when it should go away.
 *
 * Pure, and in `src/lib` on purpose: the vitest setup here runs Node against this directory, so
 * the decisions a crash screen makes can be tested without a device, a Metro bundler or a React
 * Native renderer. The component that draws it stays as thin as `cameraTransport.ts` — the same
 * split this codebase already uses for anything that cannot run off-device.
 *
 * The decisions are small and each is wrong in a way nobody would notice from a screenshot: a
 * boundary that never resets follows the operator to every other tab, and a boundary that resets
 * on every render flickers between the fallback and the crash instead of settling.
 */

/** What the fallback shows. */
export interface CrashCopy {
  title: string
  /** The reassurance, which is the question an operator on site actually has. */
  reassurance: string
  /** What to do next. */
  advice: string
  /** The error itself, for a report. Never empty — see {@link describeCrash}. */
  detail: string
}

/**
 * True when a boundary showing an error should clear itself.
 *
 * Only on a *change* of key. Resetting whenever the key is merely present would clear the
 * fallback on the next render and drop the operator straight back into the crash, over and over;
 * never resetting leaves the fallback in place on every tab they switch to until they force-quit
 * the app, which turns one broken screen into a broken tool.
 */
export function shouldReset(
  hasError: boolean,
  previousKey: string | undefined,
  nextKey: string | undefined,
): boolean {
  return hasError && previousKey !== nextKey
}

/**
 * The copy for a crash, given what threw.
 *
 * `scope` separates the two places a boundary sits. Inside the tab bar the operator still has
 * somewhere to go, so the advice says so; around the whole app there is nothing left to navigate
 * with and saying "switch tabs" would be advice to do something impossible.
 */
export function describeCrash(error: unknown, scope: 'screen' | 'app' = 'screen'): CrashCopy {
  return {
    title: scope === 'app' ? 'SiteScope could not start' : 'This screen stopped working',
    // First, and deliberately. A capture lives in SQLite and on the device's own filesystem, so
    // it survives a crash, a restart and a reinstall-free relaunch — but a screen that has just
    // vanished says none of that, and the reasonable assumption after walking a floor is that
    // the walk is gone.
    reassurance:
      'Your captures are safe. Everything recorded on this device is still stored here and '
      + 'will upload when there is a connection.',
    advice:
      scope === 'app'
        ? 'Close and reopen the app. Nothing in the queue is lost by doing that.'
        : 'Switch to another tab and back, or try this screen again.',
    detail: describeError(error),
  }
}

/**
 * A readable one-liner for whatever was thrown.
 *
 * `throw` accepts anything, and a screen that renders an empty string tells the operator
 * strictly less than one that admits it has nothing — so this never returns "".
 */
export function describeError(error: unknown): string {
  if (error instanceof Error) {
    const message = error.message.trim()
    return message ? `${error.name}: ${message}` : `${error.name} (no message)`
  }
  if (typeof error === 'string' && error.trim()) return error.trim()
  return 'An error with no description was thrown.'
}
