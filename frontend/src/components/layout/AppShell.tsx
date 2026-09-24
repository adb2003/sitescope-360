/**
 * Application shell: navigation, project context, theme, and the sign-in gate (§3, §36).
 *
 * The sidebar carries the §3 navigation. Items that need a *selected project* are grouped and
 * disabled until one is chosen, rather than hidden — a site engineer who lands on /timeline
 * should learn that a project is required, not find the item missing.
 */

import {
  Building2,
  Camera,
  CircleDot,
  ClipboardList,
  Columns2,
  FileText,
  GaugeCircle,
  LayoutDashboard,
  LogOut,
  Map,
  Menu,
  Moon,
  PanelsTopLeft,
  Settings,
  Sun,
  X,
  type LucideIcon,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'

import { ErrorBoundary } from '@/components/ErrorBoundary'
import { Badge, Button, Spinner } from '@/components/ui'
import { useAuth } from '@/hooks/useAuth'
import { useCaptureEvents } from '@/hooks/useCaptureEvents'
import { useProjectContext } from '@/hooks/useProjectContext'
import { useProjects } from '@/hooks/queries'
import { cn, humanize, initials } from '@/lib/utils'

interface NavItem {
  to: string
  label: string
  icon: LucideIcon
  /** Requires a selected project. */
  scoped?: boolean
}

const PRIMARY_NAV: NavItem[] = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/projects', label: 'Projects', icon: Building2 },
  { to: '/capture', label: 'Site Capture', icon: Camera },
]

const PROJECT_NAV: NavItem[] = [
  { to: '/floorplans', label: 'Floor Plans', icon: Map, scoped: true },
  { to: '/viewer', label: '360 Viewer', icon: PanelsTopLeft, scoped: true },
  { to: '/timeline', label: 'Timeline', icon: GaugeCircle, scoped: true },
  { to: '/compare', label: 'Compare', icon: Columns2, scoped: true },
  { to: '/annotations', label: 'Annotations', icon: ClipboardList, scoped: true },
  { to: '/reports', label: 'Reports', icon: FileText, scoped: true },
]

const SETTINGS_NAV: NavItem[] = [{ to: '/settings', label: 'Settings', icon: Settings }]

export function AppShell() {
  const { session, loading, signOut } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [mobileOpen, setMobileOpen] = useState(false)
  const [dark, setDark] = useState(() => !document.documentElement.classList.contains('light'))

  // One socket per tab, opened here because every page cares about the same events. Its return
  // value is deliberately unused: the pages underneath poll, and nothing may depend on the
  // socket being up.
  useCaptureEvents(Boolean(session))

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    document.documentElement.classList.toggle('light', !dark)
  }, [dark])

  // Close the mobile drawer on navigation, otherwise it covers the page just navigated to.
  useEffect(() => setMobileOpen(false), [location.pathname])

  useEffect(() => {
    if (!loading && !session) navigate('/login', { replace: true })
  }, [loading, session, navigate])

  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center gap-2 text-ink-muted">
        <Spinner />
        Loading your session…
      </div>
    )
  }
  if (!session) return null

  return (
    <div className="flex min-h-screen bg-canvas">
      {/* Mobile drawer backdrop */}
      {mobileOpen ? (
        <button
          type="button"
          aria-label="Close navigation"
          className="fixed inset-0 z-30 bg-black/50 lg:hidden"
          onClick={() => setMobileOpen(false)}
        />
      ) : null}

      <aside
        className={cn(
          'fixed inset-y-0 left-0 z-40 flex w-60 shrink-0 flex-col border-r border-line bg-surface transition-transform lg:static lg:translate-x-0',
          mobileOpen ? 'translate-x-0' : '-translate-x-full',
        )}
      >
        <div className="flex items-center gap-2 border-b border-line px-3 py-3">
          <div className="flex size-7 items-center justify-center rounded bg-brand text-brand-ink">
            <CircleDot className="size-4" aria-hidden />
          </div>
          <div className="min-w-0">
            <p className="truncate text-sm font-semibold tracking-tight text-ink">SiteScope 360</p>
            <p className="text-[10px] uppercase tracking-wider text-ink-subtle">
              Site monitoring
            </p>
          </div>
          <Button
            variant="ghost"
            size="icon-sm"
            className="ml-auto lg:hidden"
            onClick={() => setMobileOpen(false)}
            aria-label="Close navigation"
          >
            <X aria-hidden />
          </Button>
        </div>

        <ProjectSwitcher />

        <nav className="min-h-0 flex-1 overflow-y-auto px-2 py-2" aria-label="Main">
          <NavGroup items={PRIMARY_NAV} />
          <NavGroup label="Project" items={PROJECT_NAV} />
          <NavGroup label="Admin" items={SETTINGS_NAV} />
        </nav>

        <div className="border-t border-line p-2">
          <div className="flex items-center gap-2 rounded px-2 py-1.5">
            <div className="flex size-7 shrink-0 items-center justify-center rounded-full bg-surface-raised text-2xs font-semibold text-ink-muted">
              {initials(session.user.full_name)}
            </div>
            <div className="min-w-0 flex-1">
              <p className="truncate text-xs font-medium text-ink">{session.user.full_name}</p>
              <p className="truncate text-[10px] text-ink-subtle">
                {humanize(session.user.role)}
              </p>
            </div>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => setDark((value) => !value)}
              aria-label={dark ? 'Switch to light theme' : 'Switch to dark theme'}
              title={dark ? 'Light theme' : 'Dark theme'}
            >
              {dark ? <Sun aria-hidden /> : <Moon aria-hidden />}
            </Button>
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={() => void signOut()}
              aria-label="Sign out"
              title="Sign out"
            >
              <LogOut aria-hidden />
            </Button>
          </div>
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-20 flex items-center gap-2 border-b border-line bg-canvas/92 px-3 py-2 backdrop-blur lg:hidden">
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => setMobileOpen(true)}
            aria-label="Open navigation"
          >
            <Menu aria-hidden />
          </Button>
          <Link to="/" className="text-sm font-semibold text-ink">
            SiteScope 360
          </Link>
        </header>

        <main className="min-w-0 flex-1">
          {/* Inside the shell, so a page that throws leaves the navigation intact — an operator
              whose viewer crashed can still reach the upload queue and see their capture is
              safe. Keyed on the path so walking away from the broken page clears it, rather than
              the fallback following them around until they reload. */}
          <ErrorBoundary resetKey={location.pathname}>
            <Outlet />
          </ErrorBoundary>
        </main>
      </div>
    </div>
  )
}

function NavGroup({ label, items }: { label?: string; items: NavItem[] }) {
  const { projectId } = useProjectContext()

  return (
    <div className="mb-3">
      {label ? <p className="label px-2 py-1">{label}</p> : null}
      <ul className="space-y-0.5">
        {items.map((item) => {
          const disabled = Boolean(item.scoped && !projectId)
          const Icon = item.icon
          return (
            <li key={item.to}>
              {disabled ? (
                <span
                  className="flex cursor-not-allowed items-center gap-2 rounded px-2 py-1.5 text-sm text-ink-subtle/60"
                  title="Select a project first"
                >
                  <Icon className="size-4" aria-hidden />
                  {item.label}
                </span>
              ) : (
                <NavLink
                  to={item.to}
                  end={item.to === '/'}
                  className={({ isActive }) =>
                    cn(
                      'flex items-center gap-2 rounded px-2 py-1.5 text-sm transition-colors',
                      isActive
                        ? 'bg-brand-soft font-medium text-brand'
                        : 'text-ink-muted hover:bg-surface-raised hover:text-ink',
                    )
                  }
                >
                  <Icon className="size-4" aria-hidden />
                  {item.label}
                </NavLink>
              )}
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function ProjectSwitcher() {
  const { projectId, setProjectId } = useProjectContext()
  const { data, isLoading } = useProjects()
  const projects = data?.items ?? []

  return (
    <div className="border-b border-line px-2 py-2">
      <label htmlFor="project-switcher" className="label mb-1 block px-1">
        Active project
      </label>
      <div className="relative">
        <select
          id="project-switcher"
          value={projectId ?? ''}
          onChange={(event) => setProjectId(event.target.value || null)}
          disabled={isLoading}
          className="w-full appearance-none rounded border border-line-strong bg-surface px-2 py-1.5 text-sm text-ink"
        >
          <option value="">
            {isLoading ? 'Loading…' : projects.length ? 'Select a project' : 'No projects yet'}
          </option>
          {projects.map((project) => (
            <option key={project.id} value={project.id}>
              {project.code} — {project.name}
            </option>
          ))}
        </select>
      </div>
      {projectId ? (
        <ProjectContextBadges projects={projects} projectId={projectId} />
      ) : null}
    </div>
  )
}

function ProjectContextBadges({
  projects,
  projectId,
}: {
  projects: Array<{ id: string; floor_count: number; capture_session_count: number }>
  projectId: string
}) {
  const project = projects.find((item) => item.id === projectId)
  if (!project) return null
  return (
    <div className="mt-1.5 flex flex-wrap gap-1 px-0.5">
      <Badge tone="neutral">{project.floor_count} floors</Badge>
      <Badge tone="neutral">{project.capture_session_count} captures</Badge>
    </div>
  )
}

/** Page header used by every route, so titles, subtitles and actions align across the app. */
export function PageHeader({
  title,
  subtitle,
  actions,
  breadcrumbs,
}: {
  title: string
  subtitle?: React.ReactNode
  actions?: React.ReactNode
  breadcrumbs?: Array<{ label: string; to?: string }>
}) {
  return (
    <div className="border-b border-line bg-surface/50 px-4 py-3 lg:px-6">
      {breadcrumbs?.length ? (
        <nav aria-label="Breadcrumb" className="mb-1 flex items-center gap-1 text-2xs text-ink-subtle">
          {breadcrumbs.map((crumb, index) => (
            <span key={`${crumb.label}-${index}`} className="flex items-center gap-1">
              {index > 0 ? <span aria-hidden>/</span> : null}
              {crumb.to ? (
                <Link to={crumb.to} className="hover:text-ink">
                  {crumb.label}
                </Link>
              ) : (
                <span>{crumb.label}</span>
              )}
            </span>
          ))}
        </nav>
      ) : null}
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="truncate text-2xl font-semibold tracking-tight text-ink">{title}</h1>
          {subtitle ? <div className="mt-0.5 text-sm text-ink-muted">{subtitle}</div> : null}
        </div>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
    </div>
  )
}

export function PageBody({
  children,
  className,
}: {
  children: React.ReactNode
  className?: string
}) {
  return <div className={cn('p-4 lg:p-6', className)}>{children}</div>
}
