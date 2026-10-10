import { Component, Suspense, useEffect, useRef, useState, type ErrorInfo, type ReactNode } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router'
import type { Scope } from '../api/types'
import { useAuth } from '../auth/AuthProvider'
import { useWorkspace } from '../workspace/WorkspaceProvider'
import { LoadingState } from './ui'

interface NavItem {
  to: string
  label: string
  end?: boolean
  /** Shown only to principals holding this scope (the backend enforces it regardless). */
  scope?: Scope
  /** Further path prefixes that belong to this item (marks it active there too). */
  area?: string[]
}

const NAV_ITEMS: NavItem[] = [
  { to: '/', label: 'Overview', end: true },
  { to: '/candidates', label: 'Candidates' },
  { to: '/reviews', label: 'Reviews' },
  {
    to: '/inquiries',
    label: 'Seller inquiries',
    scope: 'inquiries:read',
    area: ['/replies', '/inquiry-control', '/mail-workers', '/evaluation'],
  },
  { to: '/lifecycle', label: 'Coverage & lags' },
  { to: '/sources', label: 'Sources' },
  { to: '/settings', label: 'Settings' },
]

function inArea(pathname: string, prefixes: string[] | undefined): boolean {
  return (prefixes ?? []).some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`))
}

/** A screen chunk that fails to load (offline, or a newer deployment removed it) gets a reload path. */
class ScreenBoundary extends Component<{ children: ReactNode; resetKey: string }, { failed: boolean; key: string }> {
  override state = { failed: false, key: this.props.resetKey }
  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true }
  }
  /** Navigating elsewhere clears an earlier failure. */
  static getDerivedStateFromProps(props: { resetKey: string }, state: { key: string }): { failed: boolean; key: string } | null {
    return props.resetKey === state.key ? null : { failed: false, key: props.resetKey }
  }
  override componentDidCatch(_error: unknown, _info: ErrorInfo) {
    // Nothing is logged: the error may carry URLs; the UI offers a reload instead.
  }
  override render() {
    if (!this.state.failed) return this.props.children
    return (
      <div className="panel panel-error" role="alert">
        <p className="panel-title">This screen could not be loaded</p>
        <p>The connection failed or a newer version of the dashboard was deployed.</p>
        <button type="button" className="button secondary" onClick={() => window.location.reload()}>
          Reload the page
        </button>
      </div>
    )
  }
}

export function Layout() {
  const { email, signOut } = useAuth()
  const { workspaceName, role, memberships, switchWorkspace, can } = useWorkspace()
  const [menuOpen, setMenuOpen] = useState(false)
  const location = useLocation()
  const mainRef = useRef<HTMLElement>(null)
  const shownPath = useRef(location.pathname)

  useEffect(() => {
    if (shownPath.current === location.pathname) return
    shownPath.current = location.pathname
    // Move keyboard/screen-reader focus to the new page content after navigation.
    mainRef.current?.focus()
  }, [location.pathname])

  return (
    <div className="app-shell">
      <a className="skip-link" href="#main">
        Skip to content
      </a>
      <header className="app-header">
        <div className="brand">
          <span className="brand-name">SUV deals review</span>
          <span className="workspace" data-testid="workspace-name">
            {workspaceName} <span className="badge badge-neutral">{role}</span>
          </span>
        </div>
        <button
          type="button"
          className="menu-toggle button secondary"
          aria-expanded={menuOpen}
          aria-controls="primary-nav"
          onClick={() => setMenuOpen((open) => !open)}
        >
          {menuOpen ? 'Close menu' : 'Menu'}
        </button>
        <nav id="primary-nav" aria-label="Primary" className={menuOpen ? 'nav nav-open' : 'nav'}>
          <ul>
            {NAV_ITEMS.filter((item) => !item.scope || can(item.scope)).map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.end ?? false}
                  className={({ isActive }) => (isActive || inArea(location.pathname, item.area) ? 'active' : undefined)}
                  onClick={() => setMenuOpen(false)}
                >
                  {item.label}
                </NavLink>
              </li>
            ))}
          </ul>
          <div className="account">
            {email ? <span className="muted account-email">{email}</span> : null}
            {memberships.filter((m) => m.active).length > 1 ? (
              <button type="button" className="button small secondary" onClick={switchWorkspace}>
                Switch workspace
              </button>
            ) : null}
            <button type="button" className="button small secondary" onClick={() => void signOut()}>
              Sign out
            </button>
          </div>
        </nav>
      </header>
      <main id="main" ref={mainRef} tabIndex={-1} className="app-main">
        <ScreenBoundary resetKey={location.pathname}>
          <Suspense fallback={<LoadingState label="Loading the screen" />}>
            <Outlet />
          </Suspense>
        </ScreenBoundary>
      </main>
    </div>
  )
}
