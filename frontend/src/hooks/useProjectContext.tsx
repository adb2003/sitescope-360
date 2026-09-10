/**
 * The selected project, shared across every route.
 *
 * Persisted to localStorage so a site engineer who reloads the tab on a tablet does not have to
 * re-pick their project. The value is validated against the projects the user can actually see:
 * a stale id from a deleted (or newly forbidden) project must not leave the app querying a 404
 * on every page.
 */

import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { useSearchParams } from 'react-router-dom'

const STORAGE_KEY = 'sitescope.project_id'

interface ProjectContextValue {
  projectId: string | null
  setProjectId: (id: string | null) => void
}

const ProjectContext = createContext<ProjectContextValue | null>(null)

export function ProjectContextProvider({ children }: { children: ReactNode }) {
  const [searchParams, setSearchParams] = useSearchParams()
  const [projectId, setStored] = useState<string | null>(() => {
    // A `?project=` parameter wins, so a link to a specific project works when shared.
    const fromUrl = new URLSearchParams(window.location.search).get('project')
    if (fromUrl) return fromUrl
    try {
      return localStorage.getItem(STORAGE_KEY)
    } catch {
      return null
    }
  })

  useEffect(() => {
    try {
      if (projectId) localStorage.setItem(STORAGE_KEY, projectId)
      else localStorage.removeItem(STORAGE_KEY)
    } catch {
      // Storage unavailable (private window): the selection still works for this session.
    }
  }, [projectId])

  const value = useMemo<ProjectContextValue>(
    () => ({
      projectId,
      setProjectId: (id) => {
        setStored(id)
        // Keep the URL in step so the current view is linkable.
        const next = new URLSearchParams(searchParams)
        if (id) next.set('project', id)
        else next.delete('project')
        setSearchParams(next, { replace: true })
      },
    }),
    [projectId, searchParams, setSearchParams],
  )

  return <ProjectContext.Provider value={value}>{children}</ProjectContext.Provider>
}

export function useProjectContext(): ProjectContextValue {
  const context = useContext(ProjectContext)
  if (!context) throw new Error('useProjectContext must be used inside <ProjectContextProvider>')
  return context
}
