/**
 * When a queued capture is ready to be handed to the processing pipeline.
 *
 * Its own module, importing nothing from React Native or Expo, so the rule can be tested under
 * plain Node. `queue.ts` cannot be: it pulls in expo-sqlite and expo-file-system, and a test that
 * had to boot those would be testing the database rather than the decision.
 *
 * The decision matters more than it looks. Before it existed the chain stopped halfway — bytes
 * reached the server and nothing happened to them, so a walkthrough somebody physically walked
 * did not exist as far as the office was concerned. Uploading was never the goal; being able to
 * *see* the walkthrough is.
 */

/** The shape this rule needs from a queued session. Structural, so `queue.ts` owns the real type. */
export interface ProcessableSession {
  id: string
  /** Null while the operator is still recording. */
  endedAt: string | null
  /** 0 until the backend has been told this session exists. */
  synced: number
}

export interface ProcessableFile {
  status: string
}

/**
 * Which sessions should be processed now.
 *
 * Two conditions, both necessary:
 *
 * - **synced**, because the server has never heard of a session that was not created there, so
 *   the call would 404.
 * - **finished, with every file uploaded**, because the pipeline maps a track *across* frames.
 *   Run on a session still recording, or on half its files, it produces a shorter path rather
 *   than a partial one — a walkthrough that looks complete and is not, which is worse than one
 *   that is obviously still arriving.
 */
export function sessionsReadyForProcessing(
  sessions: readonly ProcessableSession[],
  filesBySession: ReadonlyMap<string, readonly ProcessableFile[]>,
): string[] {
  const ready: string[] = []
  for (const session of sessions) {
    if (session.synced === 0 || !session.endedAt) continue
    const files = filesBySession.get(session.id) ?? []
    if (files.length === 0) continue
    if (files.some((file) => file.status !== 'uploaded')) continue
    ready.push(session.id)
  }
  return ready
}

/**
 * Is this failure just "the server is already on it"?
 *
 * Sync runs every time connectivity returns, so asking twice is the normal case rather than a
 * fault. Reporting it as an error would train the operator to ignore the error list, which is
 * where the failures that *do* need them live.
 */
export function isAlreadyProcessing(error: unknown): boolean {
  if (error === null || error === undefined) return false
  const status = (error as { status?: unknown }).status
  if (status === 409) return true
  const message = error instanceof Error ? error.message : String(error)
  return /already being processed/i.test(message)
}
