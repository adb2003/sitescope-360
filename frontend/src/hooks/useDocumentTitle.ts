/**
 * Name the browser tab after the page you are on, and the job you are on it for.
 *
 * Every route set the same title — "SiteScope 360" — which is wrong in three places at once. The
 * tab bar, where this product is genuinely used with two jobs open side by side to compare them,
 * and both tabs read identically. The history menu, which becomes a column of the same word. And
 * a screen reader, which announces the document title on navigation and so announced nothing.
 *
 * Called from `PageHeader` rather than by each page, so the tab and the `h1` cannot drift apart:
 * they are the same string. The project code is included when one is selected, because that is
 * the half that tells two tabs apart.
 */

import { useEffect } from 'react'

export const APP_NAME = 'SiteScope 360'

/** The title for one page, in the order a truncated tab shows first. */
export function documentTitleFor(page: string | undefined, project?: string | null): string {
  const parts = [page?.trim(), project?.trim(), APP_NAME].filter(
    (part): part is string => Boolean(part),
  )
  // De-duplicated: the dashboard of a project called "SiteScope 360" should not say it twice.
  return [...new Set(parts)].join(' · ')
}

export function useDocumentTitle(page: string | undefined, project?: string | null): void {
  useEffect(() => {
    document.title = documentTitleFor(page, project)
    // Deliberately not restored on unmount. The next page sets its own, and a restore would
    // race it — leaving the old title on screen after a navigation, which is the bug this fixes.
  }, [page, project])
}
