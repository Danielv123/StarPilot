import type { ReactNode } from 'react'
import {
  Activity,
  Archive,
  Boxes,
  CarFront,
  ChevronRight,
  CircleUserRound,
  Gauge,
  HardDriveUpload,
  Menu,
  Settings,
  SlidersHorizontal,
  X,
} from 'lucide-react'
import { NavLink } from 'react-router'
import { useState } from 'react'
import { demoMode } from '../api/client'

const links = [
  { to: '/', label: 'Overview', icon: Gauge, end: true },
  { to: '/devices', label: 'Devices', icon: CarFront },
  { to: '/uploads', label: 'Uploads', icon: HardDriveUpload },
  { to: '/drives', label: 'Drives', icon: Archive },
  { to: '/models', label: 'Models', icon: SlidersHorizontal },
  { to: '/activity', label: 'Activity', icon: Activity },
  { to: '/settings', label: 'Settings', icon: Settings },
]

export function AppShell({
  children,
  username,
  onLogout,
}: {
  children: ReactNode
  username?: string
  onLogout: () => void
}) {
  const [mobileOpen, setMobileOpen] = useState(false)

  return (
    <div className="app-shell">
      <aside className={`side-nav ${mobileOpen ? 'side-nav-open' : ''}`}>
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">
            <span />
            <span />
          </div>
          <div>
            <strong>comma</strong>
            <span>companion</span>
          </div>
          <button
            type="button"
            className="icon-button mobile-close"
            aria-label="Close navigation"
            onClick={() => setMobileOpen(false)}
          >
            <X size={19} />
          </button>
        </div>

        <nav aria-label="Primary">
          <div className="nav-section-label">Archive</div>
          {links.slice(0, 5).map(({ to, label, icon: Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              onClick={() => setMobileOpen(false)}
              className={({ isActive }) => (isActive ? 'nav-link nav-link-active' : 'nav-link')}
            >
              <Icon size={18} strokeWidth={1.8} />
              <span>{label}</span>
              <ChevronRight className="nav-chevron" size={14} />
            </NavLink>
          ))}
          <div className="nav-section-label nav-section-lower">System</div>
          {links.slice(5).map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              onClick={() => setMobileOpen(false)}
              className={({ isActive }) => (isActive ? 'nav-link nav-link-active' : 'nav-link')}
            >
              <Icon size={18} strokeWidth={1.8} />
              <span>{label}</span>
              <ChevronRight className="nav-chevron" size={14} />
            </NavLink>
          ))}
        </nav>

        <div className="nav-footer">
          <div className="nav-footer-row">
            <span className="live-indicator"><i /> Server online</span>
            <span>v0.1</span>
          </div>
          {demoMode && (
            <div className="demo-flag">
              <Boxes size={14} />
              Explicit demo data
            </div>
          )}
        </div>
      </aside>

      {mobileOpen && <button className="nav-scrim" aria-label="Close navigation" onClick={() => setMobileOpen(false)} />}

      <div className="content-shell">
        <header className="top-bar">
          <button type="button" className="icon-button mobile-menu" aria-label="Open navigation" onClick={() => setMobileOpen(true)}>
            <Menu size={20} />
          </button>
          <div className="top-context">
            <span className="top-context-dot" />
            PRIVATE ARCHIVE
          </div>
          <div className="top-account">
            <div>
              <strong>{username ?? 'Administrator'}</strong>
              <span>comma.danielv.no</span>
            </div>
            <button type="button" className="account-button" onClick={onLogout} aria-label="Sign out">
              <CircleUserRound size={22} />
            </button>
          </div>
        </header>
        <main className="app-main">{children}</main>
      </div>
    </div>
  )
}
