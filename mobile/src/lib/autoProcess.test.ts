/**
 * Tests for the step that makes a mobile upload visible in the web app.
 *
 * This was the missing link: bytes reached the server and nothing happened to them. No frames
 * extracted, no quality scored, no positions estimated — so a walkthrough somebody physically
 * walked did not exist as far as the office was concerned, and the operator had no way to know
 * that from the phone.
 *
 * The conditions are worth pinning precisely, because processing too early is as wrong as not
 * processing at all: run on a session still recording, or on half its files, the pipeline yields
 * a *shorter path* rather than a partial one — a walkthrough that looks complete and is not.
 */

import { describe, expect, it } from 'vitest'

import { isAlreadyProcessing, sessionsReadyForProcessing } from './syncPolicy'
import type { ProcessableFile, ProcessableSession } from './syncPolicy'

function session(id: string, overrides: Partial<ProcessableSession> = {}): ProcessableSession {
  return { id, endedAt: '2026-09-11T10:00:00Z', synced: 1, ...overrides }
}

function file(status: string): ProcessableFile {
  return { status }
}

describe('sessionsReadyForProcessing', () => {
  it('picks a finished session whose files are all uploaded', () => {
    const sessions = [session('CAP-1')]
    const files = new Map([['CAP-1', [file('uploaded'), file('uploaded')]]])

    expect(sessionsReadyForProcessing(sessions, files)).toEqual(['CAP-1'])
  })

  it('leaves a session that is still recording alone', () => {
    // More files are coming; processing now would publish a partial walk as a whole one.
    const sessions = [session('CAP-2', { endedAt: null })]
    const files = new Map([['CAP-2', [file('uploaded')]]])

    expect(sessionsReadyForProcessing(sessions, files)).toEqual([])
  })

  it.each([['pending'], ['uploading'], ['failed']] as const)(
    'waits while any file is still %s',
    (status) => {
      const sessions = [session('CAP-3')]
      const files = new Map([['CAP-3', [file('uploaded'), file(status)]]])

      expect(sessionsReadyForProcessing(sessions, files)).toEqual([])
    },
  )

  it('ignores a session the server has never heard of', () => {
    // Unsynced means no such session exists server-side; the call would 404.
    const sessions = [session('CAP-4', { synced: 0 })]
    const files = new Map([['CAP-4', [file('uploaded')]]])

    expect(sessionsReadyForProcessing(sessions, files)).toEqual([])
  })

  it('ignores a finished session with no files at all', () => {
    const sessions = [session('CAP-5')]
    const files = new Map([['CAP-5', [] as ProcessableFile[]]])

    expect(sessionsReadyForProcessing(sessions, files)).toEqual([])
  })

  it('ignores a session with no file record at all', () => {
    expect(sessionsReadyForProcessing([session('CAP-6')], new Map())).toEqual([])
  })

  it('judges each session independently', () => {
    const sessions = [
      session('CAP-7'),
      session('CAP-8', { endedAt: null }),
      session('CAP-9'),
      session('CAP-10', { synced: 0 }),
    ]
    const files = new Map([
      ['CAP-7', [file('uploaded')]],
      ['CAP-8', [file('uploaded')]],
      ['CAP-9', [file('uploaded')]],
      ['CAP-10', [file('uploaded')]],
    ])

    expect(sessionsReadyForProcessing(sessions, files)).toEqual(['CAP-7', 'CAP-9'])
  })
})

describe('isAlreadyProcessing', () => {
  it('treats a 409 as the outcome wanted', () => {
    // sync() runs every time connectivity returns, so asking twice is the normal case, not a
    // failure — reporting it as one would train the operator to ignore real errors.
    expect(isAlreadyProcessing({ status: 409 })).toBe(true)
  })

  it('recognises the server’s wording when no status is available', () => {
    expect(isAlreadyProcessing(new Error('This capture is already being processed'))).toBe(true)
  })

  it('does not swallow a real failure', () => {
    expect(isAlreadyProcessing({ status: 500 })).toBe(false)
    expect(isAlreadyProcessing(new Error('Internal Server Error'))).toBe(false)
    expect(isAlreadyProcessing(null)).toBe(false)
    expect(isAlreadyProcessing(undefined)).toBe(false)
  })
})
