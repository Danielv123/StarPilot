import { useEffect, useState } from 'react'
import { ArchiveX, LoaderCircle } from 'lucide-react'
import { Navigate, Route, Routes, useLocation } from 'react-router'
import { api, ApiError, isSessionExpiryError, sessionExpiredEvent } from './api/client'
import type { Session } from './api/types'
import { AppShell } from './components/AppShell'
import ActivityPage from './pages/ActivityPage'
import DeviceDetailPage from './pages/DeviceDetailPage'
import DevicesPage from './pages/DevicesPage'
import DrivesPage from './pages/DrivesPage'
import DriveStudioPage from './pages/DriveStudioPage'
import LoginPage from './pages/LoginPage'
import ModelsPage from './pages/ModelsPage'
import OverviewPage from './pages/OverviewPage'
import SettingsPage from './pages/SettingsPage'
import UploadsPage from './pages/UploadsPage'

function NotFoundPage() {
  return (
    <div className="state-panel not-found">
      <ArchiveX className="state-icon" />
      <strong>That archive view does not exist</strong>
      <span>The route may have moved or the drive ID is incomplete.</span>
      <a className="button button-primary" href="/">Return to overview</a>
    </div>
  )
}

function AppRoutes() {
  const location = useLocation()
  useEffect(() => {
    window.scrollTo({ top: 0, behavior: 'instant' })
  }, [location.pathname])

  return (
    <Routes>
      <Route path="/" element={<OverviewPage />} />
      <Route path="/devices" element={<DevicesPage />} />
      <Route path="/devices/:deviceId" element={<DeviceDetailPage />} />
      <Route path="/uploads" element={<UploadsPage />} />
      <Route path="/drives" element={<DrivesPage />} />
      <Route path="/drives/:driveId" element={<DriveStudioPage />} />
      <Route path="/models" element={<ModelsPage />} />
      <Route path="/activity" element={<ActivityPage />} />
      <Route path="/settings" element={<SettingsPage />} />
      <Route path="/login" element={<Navigate to="/" replace />} />
      <Route path="*" element={<NotFoundPage />} />
    </Routes>
  )
}

export default function App() {
  const [session, setSession] = useState<Session>()
  const [loading, setLoading] = useState(true)
  const [sessionError, setSessionError] = useState<Error>()

  useEffect(() => {
    const expireSession = () => {
      setSessionError(undefined)
      setSession({ authenticated: false })
      setLoading(false)
    }
    window.addEventListener(sessionExpiredEvent, expireSession)
    return () => window.removeEventListener(sessionExpiredEvent, expireSession)
  }, [])

  useEffect(() => {
    let active = true
    void api.session()
      .then((result) => {
        if (active) setSession(result)
      })
      .catch((error) => {
        if (!active) return
        if (error instanceof ApiError && (error.status === 401 || error.status === 403)) {
          setSession({ authenticated: false })
        } else {
          setSessionError(error instanceof Error ? error : new Error('Session check failed'))
        }
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    return () => { active = false }
  }, [])

  if (loading) {
    return (
      <main className="boot-screen">
        <div className="brand">
          <div className="brand-mark"><span /><span /></div>
          <div><strong>comma</strong><span>companion</span></div>
        </div>
        <LoaderCircle className="spin" />
        <span>Opening private archive…</span>
      </main>
    )
  }

  if (sessionError) {
    return (
      <main className="boot-screen boot-error">
        <ArchiveX />
        <strong>Archive server unavailable</strong>
        <span>{sessionError.message}</span>
        <button className="button button-secondary" onClick={() => window.location.reload()}>Retry</button>
      </main>
    )
  }

  if (!session?.authenticated) {
    return <LoginPage onAuthenticated={setSession} />
  }

  return (
    <AppShell
      username={session.username}
      onLogout={() => {
        void api.logout()
          .then(() => setSession({ authenticated: false }))
          .catch((error) => {
            if (isSessionExpiryError(error)) {
              setSession({ authenticated: false })
            } else {
              setSessionError(
                error instanceof Error
                  ? error
                  : new Error('The server did not confirm logout. Your session is still active.'),
              )
            }
          })
      }}
    >
      <AppRoutes />
    </AppShell>
  )
}
