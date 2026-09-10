/** Report generation and download (§29). */

import { Download, FileText, Plus } from 'lucide-react'
import { useState, type FormEvent } from 'react'

import { PageBody, PageHeader } from '@/components/layout/AppShell'
import {
  Badge,
  Button,
  Callout,
  Card,
  CardHeader,
  Dialog,
  EmptyState,
  ErrorState,
  Field,
  Input,
  Select,
  Skeleton,
  Table,
  Td,
  Th,
  Tr,
} from '@/components/ui'
import {
  useGenerateReport,
  useProjectFloors,
  useReportTypes,
  useReports,
} from '@/hooks/queries'
import { Capability, useAuth } from '@/hooks/useAuth'
import { useProjectContext } from '@/hooks/useProjectContext'
import { ApiError, api } from '@/lib/api'
import { downloadBlob, formatBytes, formatDateTime, humanize } from '@/lib/utils'
import type { Report } from '@/types/api'

export function ReportsPage() {
  const { projectId } = useProjectContext()
  const { can } = useAuth()
  const reports = useReports(projectId)
  const catalogue = useReportTypes()
  const [creating, setCreating] = useState(false)
  const [downloadError, setDownloadError] = useState<string | null>(null)

  const canGenerate = can(Capability.ReportGenerate)

  async function download(report: Report) {
    setDownloadError(null)
    try {
      // Fetched through the API client rather than a plain link, so the Authorization header is
      // attached and a rotated token still works.
      const response = await api.raw(`/reports/${report.id}/download`)
      const blob = await response.blob()
      const extension = report.report_format === 'pdf' ? 'pdf' : report.report_format
      downloadBlob(blob, `${sanitise(report.title)}.${extension}`)
    } catch (caught) {
      setDownloadError(caught instanceof ApiError ? caught.message : 'Download failed')
    }
  }

  if (!projectId) {
    return (
      <PageBody>
        <Card>
          <EmptyState
            icon={FileText}
            title="No project selected"
            description="Choose a project from the sidebar to generate its reports."
          />
        </Card>
      </PageBody>
    )
  }

  return (
    <>
      <PageHeader
        title="Reports"
        subtitle="Coverage, capture history and findings, exportable as PDF, CSV or JSON"
        actions={
          canGenerate ? (
            <Button variant="primary" icon={Plus} onClick={() => setCreating(true)}>
              Generate report
            </Button>
          ) : null
        }
      />

      <PageBody className="space-y-4">
        {downloadError ? <Callout tone="bad">{downloadError}</Callout> : null}

        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          {catalogue.data?.types.map((type) => (
            <Card key={type.key} className="p-3.5">
              <p className="text-sm font-semibold text-ink">{type.name}</p>
              <p className="mt-1 text-xs leading-relaxed text-ink-muted">{type.description}</p>
            </Card>
          ))}
        </div>

        <Callout tone="info" title="Every report states its own provenance">
          Each export carries a section explaining how its figures were produced — which are
          measured, which are administrator-declared, and that no progress percentage is inferred.
          That section survives the CSV export as comment lines.
        </Callout>

        <Card>
          <CardHeader title="Generated reports" />
          {reports.isError ? (
            <ErrorState message={reports.error?.message} onRetry={() => void reports.refetch()} />
          ) : reports.isLoading ? (
            <div className="space-y-2 p-4">
              {[0, 1].map((index) => (
                <Skeleton key={index} className="h-9" />
              ))}
            </div>
          ) : !reports.data?.items.length ? (
            <EmptyState
              icon={FileText}
              title="No reports yet"
              description="Generate one to export coverage, capture history or the annotation log."
              action={
                canGenerate ? (
                  <Button variant="primary" icon={Plus} onClick={() => setCreating(true)}>
                    Generate report
                  </Button>
                ) : null
              }
            />
          ) : (
            <Table>
              <thead>
                <tr>
                  <Th>Title</Th>
                  <Th>Type</Th>
                  <Th>Format</Th>
                  <Th>Period</Th>
                  <Th className="text-right">Size</Th>
                  <Th>Generated</Th>
                  <Th>Status</Th>
                  <Th className="w-28" />
                </tr>
              </thead>
              <tbody>
                {reports.data.items.map((report) => (
                  <Tr key={report.id}>
                    <Td className="max-w-sm truncate font-medium">{report.title}</Td>
                    <Td className="text-xs text-ink-muted">{humanize(report.report_type)}</Td>
                    <Td>
                      <Badge tone="neutral">{report.report_format.toUpperCase()}</Badge>
                    </Td>
                    <Td className="text-xs text-ink-muted">
                      {report.period_start || report.period_end
                        ? `${report.period_start ?? '…'} → ${report.period_end ?? 'now'}`
                        : 'All time'}
                    </Td>
                    <Td className="tnum text-right text-xs">
                      {formatBytes(report.file_size_bytes)}
                    </Td>
                    <Td className="text-xs text-ink-muted">
                      {formatDateTime(report.generated_at)}
                    </Td>
                    <Td>
                      <Badge
                        tone={
                          report.status === 'ready'
                            ? 'good'
                            : report.status === 'failed'
                              ? 'bad'
                              : 'info'
                        }
                      >
                        {humanize(report.status)}
                      </Badge>
                    </Td>
                    <Td>
                      <Button
                        size="sm"
                        variant="secondary"
                        icon={Download}
                        disabled={report.status !== 'ready'}
                        onClick={() => void download(report)}
                      >
                        Download
                      </Button>
                    </Td>
                  </Tr>
                ))}
              </tbody>
            </Table>
          )}
        </Card>
      </PageBody>

      {creating ? (
        <GenerateDialog projectId={projectId} onClose={() => setCreating(false)} />
      ) : null}
    </>
  )
}

function GenerateDialog({ projectId, onClose }: { projectId: string; onClose: () => void }) {
  const mutation = useGenerateReport(projectId)
  const catalogue = useReportTypes()
  const floors = useProjectFloors(projectId)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    const form = new FormData(event.currentTarget)
    try {
      await mutation.mutateAsync({
        report_type: String(form.get('report_type')),
        report_format: String(form.get('report_format')),
        floor_id: emptyToUndefined(form.get('floor_id')),
        period_start: emptyToUndefined(form.get('period_start')),
        period_end: emptyToUndefined(form.get('period_end')),
      })
      onClose()
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : 'Report generation failed')
    }
  }

  return (
    <Dialog open onClose={onClose} title="Generate report">
      <form onSubmit={onSubmit} className="space-y-3">
        <Field label="Report type" required>
          <Select name="report_type" required defaultValue="project_summary">
            {catalogue.data?.types.map((type) => (
              <option key={type.key} value={type.key}>
                {type.name}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Format" required>
          <Select name="report_format" required defaultValue="pdf">
            {(catalogue.data?.formats ?? ['pdf', 'csv', 'json']).map((format) => (
              <option key={format} value={format}>
                {format.toUpperCase()}
              </option>
            ))}
          </Select>
        </Field>

        <Field label="Floor" hint="Optional — leave blank for the whole project">
          <Select name="floor_id">
            <option value="">All floors</option>
            {floors.data?.map((floor) => (
              <option key={floor.id} value={floor.id}>
                {floor.building_name} · {floor.name}
              </option>
            ))}
          </Select>
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Period start" hint="Optional">
            <Input name="period_start" type="date" />
          </Field>
          <Field label="Period end" hint="Optional">
            <Input name="period_end" type="date" />
          </Field>
        </div>

        {error ? <Callout tone="bad">{error}</Callout> : null}

        <div className="flex justify-end gap-2">
          <Button type="button" variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button type="submit" variant="primary" loading={mutation.isPending}>
            Generate
          </Button>
        </div>
      </form>
    </Dialog>
  )
}

function emptyToUndefined(value: FormDataEntryValue | null): string | undefined {
  const text = String(value ?? '').trim()
  return text ? text : undefined
}

function sanitise(title: string): string {
  return title.replace(/[^a-z0-9 \-_]/gi, '_').trim() || 'report'
}
