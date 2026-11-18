import { useState } from 'react'
import { Archive, Eye, EyeOff, LockKeyhole, ShieldCheck } from 'lucide-react'
import { api } from '../api/client'
import type { Session } from '../api/types'

export default function LoginPage({ onAuthenticated }: { onAuthenticated: (session: Session) => void }) {
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [showPassword, setShowPassword] = useState(false)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<Error>()

  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    setLoading(true)
    setError(undefined)
    try {
      onAuthenticated(await api.login(username, password))
    } catch (caught) {
      setError(caught instanceof Error ? caught : new Error('Sign-in failed'))
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="login-page">
      <section className="login-brand-panel">
        <div className="login-brand">
          <div className="brand-mark"><span /><span /></div>
          <div><strong>comma</strong><span>companion</span></div>
        </div>
        <div className="login-statement">
          <div className="eyebrow">Private driving archive</div>
          <h1>Your drives.<br />Your telemetry.<br />Your models.</h1>
          <p>Durable server-side playback and dynamics replay, isolated from the upstream driving stack.</p>
        </div>
        <div className="login-feature-row">
          <span><Archive /> Immutable source archive</span>
          <span><ShieldCheck /> Outbound-only device link</span>
        </div>
      </section>
      <section className="login-form-panel">
        <form className="login-form" onSubmit={(event) => void submit(event)}>
          <div className="login-lock"><LockKeyhole /></div>
          <div>
            <div className="eyebrow">comma.danielv.no</div>
            <h2>Archive sign in</h2>
            <p>Use the private administrator account.</p>
          </div>
          {error && <div className="notice notice-error" role="alert">{error.message}</div>}
          <label className="field">
            <span>Username</span>
            <input autoFocus autoComplete="username" value={username} onChange={(event) => setUsername(event.target.value)} />
          </label>
          <label className="field">
            <span>Password</span>
            <div className="password-field">
              <input type={showPassword ? 'text' : 'password'} autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} />
              <button type="button" aria-label={showPassword ? 'Hide password' : 'Show password'} onClick={() => setShowPassword((value) => !value)}>
                {showPassword ? <EyeOff size={17} /> : <Eye size={17} />}
              </button>
            </div>
          </label>
          <button className="button button-primary button-login" disabled={loading || !username || !password}>
            {loading ? 'Authenticating…' : 'Sign in'}
          </button>
          <small>Session cookies are Secure, HttpOnly, and restricted to this site.</small>
        </form>
      </section>
    </main>
  )
}
