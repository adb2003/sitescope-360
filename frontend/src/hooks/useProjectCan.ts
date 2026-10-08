/**
 * What the signed-in person may do **on the project they are looking at** (§27).
 *
 * `useAuth().can` answers for the account, and the account role is now only a ceiling: the role
 * that decides is the one held on the project. The same site engineer is an engineer on one job
 * and a viewer on the next, so an interface built on the account alone offers an upload button on
 * a job where it returns 403 — and a button that fails is worse than no button, because the
 * person cannot tell whether they lack the permission or the product is broken.
 *
 * The capability list comes from the server (`your_capabilities` on the project payload) rather
 * than being derived from the role here. The role table lives in `app/auth/rbac.py` and a second
 * copy in TypeScript would be a second thing to keep in step — the same reason `/auth/me` sends
 * capabilities instead of a role.
 *
 * While the project is loading this returns `false`, which is the safe direction: a control that
 * appears a moment late is a smaller problem than one that appears and then refuses.
 */

import { useCallback } from 'react'

import { useProject } from '@/hooks/queries'
import { useAuth } from '@/hooks/useAuth'

export interface ProjectCan {
  /** Does the caller hold this capability on this project? */
  can: (capability: string) => boolean
  /** Their role here, for display — `null` until the project has loaded. */
  role: string | null
  loading: boolean
}

export function useProjectCan(projectId: string | null | undefined): ProjectCan {
  const { can: accountCan } = useAuth()
  const project = useProject(projectId)
  const granted = project.data?.your_capabilities

  const can = useCallback(
    (capability: string) => {
      // An administrator reading a project nobody is a member of has no membership and no
      // restriction either; the server sends an empty list with a null role, and the account
      // answer is the right one.
      if (project.data && project.data.your_role === null) return accountCan(capability)
      if (!granted) return false
      // Both must hold. The server enforces the same pair — the route guard reads the account,
      // the resolver reads the project — so a UI that checked only one would disagree with it in
      // one direction or the other.
      return granted.includes(capability) && accountCan(capability)
    },
    [accountCan, granted, project.data],
  )

  return { can, role: project.data?.your_role ?? null, loading: project.isLoading }
}
