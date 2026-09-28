/**
 * Who can see this project (§27).
 *
 * Roles say what a person may do; membership says which projects they may do it to. This panel
 * is the second half, and it exists because without it membership is enforceable but not
 * administrable — an API-only permission model means the first support request is "please run a
 * curl for me".
 *
 * People are added **by email**, not picked from a list. Listing users needs `user:manage`, which
 * a project manager does not have, so a picker would work for administrators and be empty for
 * exactly the people most likely to be staffing their own job. Typing an address is also not a
 * disclosure: you have to know it already.
 *
 * The role is shown beside each person on purpose. Membership is not a role, and a list that
 * showed only names would imply that being added grants some level of access of its own.
 */

import { Trash2, UserPlus } from 'lucide-react'
import { useState, type FormEvent } from 'react'

import {
  Badge,
  Button,
  Callout,
  Card,
  CardHeader,
  EmptyState,
  Field,
  Input,
  Skeleton,
  Table,
  Td,
  Th,
  Tr,
} from '@/components/ui'
import {
  useAddProjectMember,
  useProjectMembers,
  useRemoveProjectMember,
} from '@/hooks/queries'
import { Capability, useAuth } from '@/hooks/useAuth'
import { ApiError } from '@/lib/api'
import { formatDateTime, humanize } from '@/lib/utils'

export function ProjectAccessPanel({ projectId }: { projectId: string }) {
  const { can, session } = useAuth()
  const members = useProjectMembers(projectId)
  const add = useAddProjectMember(projectId)
  const remove = useRemoveProjectMember(projectId)
  const [email, setEmail] = useState('')
  const [error, setError] = useState<string | null>(null)
  const canManage = can(Capability.SettingsProject)

  const submit = (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    add.mutate(
      { email: email.trim() },
      {
        onSuccess: () => setEmail(''),
        // The server's refusals are specific — no such user, or a project this caller is not on
        // — and both are worth reading rather than being flattened into "could not add".
        onError: (err) =>
          setError(err instanceof ApiError ? err.message : 'Could not grant access'),
      },
    )
  }

  return (
    <Card>
      <CardHeader
        title="Project access"
        description="Who can open this project. A role says what someone may do; this says where."
      />

      <div className="p-3.5 pt-0">
        {canManage ? (
          <form onSubmit={submit} className="mb-3 flex flex-wrap items-end gap-2">
            <Field label="Add by email" className="min-w-64 flex-1">
              <Input
                type="email"
                required
                value={email}
                placeholder="colleague@example.com"
                onChange={(event) => setEmail(event.target.value)}
              />
            </Field>
            <Button
              type="submit"
              size="sm"
              icon={UserPlus}
              loading={add.isPending}
              disabled={!email.trim()}
            >
              Grant access
            </Button>
          </form>
        ) : null}

        {error ? <Callout tone="bad">{error}</Callout> : null}

        {members.isLoading ? (
          <Skeleton className="h-24" />
        ) : members.data && members.data.length > 0 ? (
          <Table>
            <thead>
              <Tr>
                <Th>Person</Th>
                <Th>Role</Th>
                <Th>Added</Th>
                {canManage ? <Th /> : null}
              </Tr>
            </thead>
            <tbody>
              {members.data.map((member) => (
                <Tr key={member.user_id}>
                  <Td>
                    <span className="block text-ink">{member.full_name}</span>
                    <span className="block text-xs text-ink-subtle">{member.email}</span>
                  </Td>
                  <Td>
                    <Badge tone={member.is_active ? 'neutral' : 'warn'}>
                      {humanize(member.role)}
                      {member.is_active ? '' : ' · deactivated'}
                    </Badge>
                  </Td>
                  <Td className="text-xs text-ink-muted">{formatDateTime(member.added_at)}</Td>
                  {canManage ? (
                    <Td className="text-right">
                      <Button
                        size="sm"
                        variant="ghost"
                        icon={Trash2}
                        loading={remove.isPending}
                        aria-label={`Remove ${member.full_name}`}
                        onClick={() => {
                          setError(null)
                          remove.mutate(member.user_id, {
                            onError: (err) =>
                              setError(
                                err instanceof ApiError ? err.message : 'Could not remove access',
                              ),
                          })
                        }}
                      >
                        {member.user_id === session?.user.id ? 'Leave' : 'Remove'}
                      </Button>
                    </Td>
                  ) : null}
                </Tr>
              ))}
            </tbody>
          </Table>
        ) : (
          /*
            An empty list is reachable and is not an error: an administrator sees every project
            regardless of membership, so a project with nobody on it still opens for them — and
            looks like this.
          */
          <EmptyState
            icon={UserPlus}
            title="Nobody has been given access yet"
            description="Administrators can open this project regardless. Add the people who need it."
          />
        )}
      </div>
    </Card>
  )
}
