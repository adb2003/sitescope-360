/**
 * Choosing which sheet of a drawing set is the floor plan (§22).
 *
 * A set arrives as one PDF holding forty drawings, of which one or two are the plans anybody
 * wants to place capture points on. The upload has always accepted a page number and the
 * operator has always had to *know* it — which, on a set emailed by a consultant months ago with
 * no consistent ordering, is a guess followed by a re-upload.
 *
 * So this shows the sheets. Thumbnails load one at a time as they come into view rather than all
 * at once, because a set can have hundreds of pages and the picker has to open immediately.
 *
 * The warning after a switch is not decoration. Capture points are stored in the pixels of the
 * plan they were placed on; switching sheets creates a new revision and does *not* move them,
 * but the editor draws every point on the floor over whichever plan is active — so points from
 * the old sheet appear on the new one at coordinates that describe a different drawing. It looks
 * entirely plausible, which is exactly why it has to be said.
 */

import { Check, Layers } from 'lucide-react'
import { useState } from 'react'

import { Badge, Button, Callout, Skeleton } from '@/components/ui'
import { SheetThumbnail } from './SheetThumbnail'
import { useChooseSheet, usePlanSheets } from '@/hooks/queries'
import { ApiError } from '@/lib/api'
import type { SheetChosen } from '@/types/api'

export function SheetPicker({ planId }: { planId: string }) {
  const sheets = usePlanSheets(planId)
  const choose = useChooseSheet(planId)
  const [result, setResult] = useState<SheetChosen | null>(null)
  const [error, setError] = useState<string | null>(null)

  if (sheets.isLoading) return <Skeleton className="h-32" />
  // Not an error state: a plan uploaded as a PNG has no sheets, and the API says so with a 422.
  // Rendering nothing is the right answer — there is no choice to offer.
  if (sheets.isError || !sheets.data) return null

  const { page_count, current_page, sheets: pages } = sheets.data

  return (
    <div className="space-y-3 p-4 pt-0">
      <div>
        <p className="text-xs font-medium text-ink">
          <Layers className="mr-1 inline size-3.5" />
          {page_count} sheets in this set
        </p>
        <p className="mt-0.5 text-xs text-ink-subtle">
          Pick the general-arrangement plan. Sections, elevations and schedules are in here too.
        </p>
      </div>

      {error ? <Callout tone="bad">{error}</Callout> : null}

      {result ? (
        <Callout
          tone={result.points_from_earlier_sheets > 0 ? 'warn' : 'info'}
          title={`Now using ${result.plan.name}`}
        >
          {result.note}
        </Callout>
      ) : null}

      <ul className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {pages.map((sheet) => {
          const active = sheet.page === current_page
          return (
            <li key={sheet.page}>
              <button
                type="button"
                disabled={active || choose.isPending}
                aria-label={`Use sheet ${sheet.page}`}
                className={`group w-full overflow-hidden rounded border text-left transition ${
                  active
                    ? 'border-brand ring-1 ring-brand'
                    : 'border-line hover:border-ink-subtle'
                }`}
                onClick={() => {
                  setError(null)
                  choose.mutate(sheet.page, {
                    onSuccess: setResult,
                    onError: (err) =>
                      setError(
                        err instanceof ApiError ? err.message : 'Could not switch to that sheet',
                      ),
                  })
                }}
              >
                <SheetThumbnail planId={planId} page={sheet.page} />
                <span className="flex items-center justify-between px-2 py-1.5 text-xs">
                  <span className="text-ink">Sheet {sheet.page}</span>
                  {active ? (
                    <Badge tone="brand">
                      <Check className="mr-0.5 size-3" />
                      in use
                    </Badge>
                  ) : (
                    <span className="text-ink-subtle">{sheet.orientation}</span>
                  )}
                </span>
              </button>
            </li>
          )
        })}
      </ul>

      {choose.isPending ? (
        <Button size="sm" variant="ghost" loading disabled>
          Rendering that sheet…
        </Button>
      ) : null}
    </div>
  )
}
