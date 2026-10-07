import { useEffect, useRef, useState } from 'react'
import { NavLink, Outlet, useLocation } from 'react-router'
import { useAuth } from '../auth/AuthProvider'
import { useWorkspace } from '../workspace/WorkspaceProvider'

const NAV_ITEMS: Array<{ to: string; label: string; end?: boolean; note?: string }> = [
  { to: '/', label: 'Overview', end: true },
  { to: '/candidates', label: 'Candidates' },
  { to: '/reviews', label: 'Reviews' },
  { to: '/sources', label: 'Sources' },
  { to: '/settings', label: 'Settings' },
  { to: '/inquiries', label: 'Seller inquiries', note: 'v1.1' },
]

export function Layout() {
  const { email, signOut } = useAuth()
  const { workspaceName, role, memberships, switchWorkspace } = useWorkspace()
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
            {NAV_ITEMS.map((item) => (
              <li key={item.to}>
                <NavLink
                  to={item.to}
                  end={item.end ?? false}
                  className={({ isActive }) => (isActive ? 'active' : undefined)}
                  onClick={() => setMenuOpen(false)}
                >
                  {item.label}
                  {item.note ? <span className="nav-note"> ({item.note})</span> : null}
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
        <Outlet />
      </main>
    </div>
  )
}
