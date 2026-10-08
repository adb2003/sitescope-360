/**
 * Who is on this project, and what they are **here** (§27).
 *
 * A role used to belong to the account, so this panel could only say who had access and let the
 * role table do the rest. Now `project_members` carries the role: the same person can run this
 * job and only watch the next, so this is where that is decided — and without it the feature
 * would be enforceable but not administrable, which means the first support request is "please
 * run a curl for me".
 *
 * People are added **by email**, not picked from a list. Listing users needs `user:manage`, which
 * a project manager does not have, so a picker would work for administrators and be empty for
 * exactly the people most likely to be staffing their own job. Typing an address is also not a
 * disclosure: you have to know it already.
 *
 * Two rules are mirrored here rather than left to the server to refuse, because a control that
 * cannot work teaches people to ignore refusals:
 *
 *   * the roles on offer stop at the one the actor holds on this project — nobody hands out more
 *     than they have, or `settings:project` would be worth `settings:system`;
 *   * a role above somebody's account role is shown as capped, because the API enforces
 *     `min(account, project)` on every request and a row reading `project_manager` for an account
 *     that says `viewer` would otherwise look like a permission they do not have.
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
  Select,
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
  useSetProjectMemberRole,
} from '@/hooks/queries'
import { Capability, useAuth } from '@/hooks/useAuth'
import { useProjectCan } from '@/hooks/useProjectCan'
import { ApiError } from '@/lib/api'
import type { Role } from '@/types/api'
import { formatDateTime, humanize } from '@/lib/utils'

/** Ascending privilege, matching `ROLE_RANK` in `app/core/enums.py`. */
const ROLES: Role[] = ['viewer', 'site_engineer', 'project_manager', 'admin']

export function ProjectAccessPanel({ projectId }: { projectId: string }) {
  const { session } = useAuth()
  const { can, role: myRole } = useProjectCan(projectId)
  const members = useProjectMembers(projectId)
  const add = useAddProjectMember(projectId)
  const remove = useRemoveProjectMember(projectId)
  const setRole = useSetProjectMemberRole(projectId)
  const [email, setEmail] = useState('')
  const [role, setNewRole] = useState<Role>('viewer')
  const [error, setError] = useState<string | null>(null)
  const canManage = can(Capability.SettingsProject)
  // What this person may hand out: their own role here and anything below it. An administrator
  // is exempt from membership, so `myRole` is their account role and the list is everything.
  const grantable = ROLES.slice(0, Math.max(ROLES.indexOf((myRole as Role) ?? 'viewer'), 0) + 1)

  const fail = (fallback: string) => (err: unknown) =>
    setError(err instanceof ApiError ? err.message : fallback)

  const submit = (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    add.mutate(
      { email: email.trim(), role },
      {
        onSuccess: () => setEmail(''),
        // The server's refusals are specific — no such user, a project this caller is not on, a
        // role above their own — and all three are worth reading rather than being flattened
        // into "could not add".
        onError: fail('Could not grant access'),
      },
    )
  }

  return (
    <Card>
      <CardHeader
        title="Project access"
        description="Who can open this project, and what they are on it. An account role is a ceiling; this is what applies here."
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
            <Field label="As" className="w-44">
              <Select
                value={role}
                aria-label="Role on this project"
                onChange={(event) => setNewRole(event.target.value as Role)}
              >
                {grantable.map((value) => (
                  <option key={value} value={value}>
                    {humanize(value)}
                  </option>
                ))}
              </Select>
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
                <Th>On this project</Th>
                <Th>Added</Th>
                {canManage ? <Th /> : null}
              </Tr>
            </thead>
            <tbody>
              {members.data.map((member) => {
                const capped = member.effective_role !== member.role
                return (
                  <Tr key={member.user_id}>
                    <Td>
                      <span className="block text-ink">{member.full_name}</span>
                      <span className="block text-xs text-ink-subtle">{member.email}</span>
                    </Td>
                    <Td>
                      {canManage ? (
                        <Select
                          className="min-w-40 py-1 text-xs"
                          aria-label={`Role for ${member.full_name} on this project`}
                          value={member.role}
                          disabled={setRole.isPending}
                          onChange={(event) => {
                            setError(null)
                            setRole.mutate(
                              { userId: member.user_id, role: event.target.value as Role },
                              { onError: fail('Could not change their role') },
                            )
                          }}
                        >
                          {/*
                            Their stored role stays selectable even when it is above what this
                            actor could grant, so the control shows the truth rather than
                            silently appearing to be something lower.
                          */}
                          {Array.from(new Set([...grantable, member.role])).map((value) => (
                            <option key={value} value={value}>
                              {humanize(value)}
                            </option>
                          ))}
                        </Select>
                      ) : (
                        <Badge tone="neutral">{humanize(member.effective_role)}</Badge>
                      )}
                      {capped ? (
                        <span className="mt-1 block text-xs text-ink-muted">
                          Capped to {humanize(member.effective_role)} — their account is{' '}
                          {humanize(member.account_role)}
                        </span>
                      ) : null}
                      {member.is_active ? null : (
                        <Badge tone="warn" className="mt-1">
                          Deactivated
                        </Badge>
                      )}
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
                              onError: fail('Could not remove access'),
                            })
                          }}
                        >
                          {member.user_id === session?.user.id ? 'Leave' : 'Remove'}
                        </Button>
                      </Td>
                    ) : null}
                  </Tr>
                )
              })}
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
