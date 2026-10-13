/**
 * The browser tab has to say which page, and which job.
 *
 * Every route set the same title — "SiteScope 360" — which breaks the workflow this product is
 * actually used for: two jobs open side by side to compare them, and two tabs that read
 * identically. It also made the history menu a column of one word, and gave a screen reader
 * nothing to announce on navigation.
 */

import { describe, expect, it } from 'vitest'

import { APP_NAME, documentTitleFor } from './useDocumentTitle'

describe('documentTitleFor', () => {
  it('names the page and the project, page first', () => {
    // Page first because a narrow tab truncates the end, and "which screen" is what a person
    // scanning a row of tabs is looking for once they already know the job.
    expect(documentTitleFor('Floor plans', 'DEMO-001')).toBe('Floor plans · DEMO-001 · SiteScope 360')
  })

  it('still names the app when no project is selected', () => {
    expect(documentTitleFor('Dashboard', null)).toBe('Dashboard · SiteScope 360')
    expect(documentTitleFor('Dashboard')).toBe('Dashboard · SiteScope 360')
  })

  it('falls back to the app alone rather than a stray separator', () => {
    expect(documentTitleFor(undefined)).toBe(APP_NAME)
    expect(documentTitleFor('   ')).toBe(APP_NAME)
  })

  it('does not say the same word twice', () => {
    // A project whose code happens to be the product name, or a page called after it.
    expect(documentTitleFor('SiteScope 360', 'SiteScope 360')).toBe('SiteScope 360')
  })
})
