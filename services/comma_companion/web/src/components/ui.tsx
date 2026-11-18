import type { ReactNode } from 'react'
import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  CloudOff,
  LoaderCircle,
  RefreshCw,
  ServerOff,
} from 'lucide-react'
import type { HealthState } from '../api/types'
import { formatRelativeTime } from '../utils'

export function StatusBadge({
  state,
  label,
  pulse = false,
}: {
  state: HealthState | string
  label?: string
  pulse?: boolean
}) {
  return (
    <span className={`status-badge status-${state}`}>
      <span className={`status-dot ${pulse ? 'status-pulse' : ''}`} aria-hidden="true" />
      {label ?? state.replaceAll('_', ' ')}
    </span>
  )
}

export function PageHeader({
  eyebrow,
  title,
  description,
  actions,
}: {
  eyebrow?: string
  title: string
  description?: string
  actions?: ReactNode
}) {
  return (
    <header className="page-header">
      <div>
        {eyebrow && <div className="eyebrow">{eyebrow}</div>}
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </header>
  )
}

export function Panel({
  children,
  className = '',
  title,
  kicker,
  action,
}: {
  children: ReactNode
  className?: string
  title?: string
  kicker?: string
  action?: ReactNode
}) {
  return (
    <section className={`panel ${className}`}>
      {(title || kicker || action) && (
        <header className="panel-header">
          <div>
            {kicker && <div className="panel-kicker">{kicker}</div>}
            {title && <h2>{title}</h2>}
          </div>
          {action && <div className="panel-action">{action}</div>}
        </header>
      )}
      {children}
    </section>
  )
}

export function Metric({
  label,
  value,
  detail,
  tone,
  icon,
  background,
}: {
  label: string
  value: ReactNode
  detail?: ReactNode
  tone?: 'good' | 'warn' | 'bad' | 'info'
  icon?: ReactNode
  background?: ReactNode
}) {
  return (
    <div className={`metric ${tone ? `metric-${tone}` : ''}`}>
      {background && <div className="metric-background" aria-hidden="true">{background}</div>}
      <div className="metric-topline">
        <span>{label}</span>
        {icon}
      </div>
      <div className="metric-value">{value}</div>
      {detail && <div className="metric-detail">{detail}</div>}
    </div>
  )
}

export function ProgressBar({
  value,
  tone = 'primary',
  label,
}: {
  value: number
  tone?: 'primary' | 'warn' | 'error' | 'muted'
  label?: string
}) {
  const bounded = Math.max(0, Math.min(100, value))
  return (
    <div
      className={`progress progress-${tone}`}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(bounded)}
      aria-label={label}
    >
      <span style={{ width: `${bounded}%` }} />
    </div>
  )
}

export function LoadingState({ label = 'Loading archive data' }: { label?: string }) {
  return (
    <div className="state-panel" role="status">
      <LoaderCircle className="state-icon spin" aria-hidden="true" />
      <strong>{label}</strong>
      <span>Reading the latest durable server state…</span>
    </div>
  )
}

export function ErrorState({
  error,
  retry,
  title = 'Archive data unavailable',
}: {
  error: Error
  retry?: () => void
  title?: string
}) {
  return (
    <div className="state-panel state-error" role="alert">
      <ServerOff className="state-icon" aria-hidden="true" />
      <strong>{title}</strong>
      <span>{error.message}</span>
      {retry && (
        <button type="button" className="button button-secondary" onClick={retry}>
          <RefreshCw size={15} />
          Try again
        </button>
      )}
    </div>
  )
}

export function EmptyState({
  title,
  description,
  icon = <CircleDashed aria-hidden="true" />,
  action,
}: {
  title: string
  description: string
  icon?: ReactNode
  action?: ReactNode
}) {
  return (
    <div className="state-panel state-empty">
      <div className="empty-icon">{icon}</div>
      <strong>{title}</strong>
      <span>{description}</span>
      {action}
    </div>
  )
}

export function ServiceIcon({ state }: { state: HealthState }) {
  if (state === 'healthy') return <CheckCircle2 className="service-icon service-good" aria-hidden="true" />
  if (state === 'running') return <LoaderCircle className="service-icon service-info spin" aria-hidden="true" />
  if (state === 'warning') return <AlertTriangle className="service-icon service-warn" aria-hidden="true" />
  return <CloudOff className="service-icon service-bad" aria-hidden="true" />
}

export function LastUpdated({ value }: { value?: string }) {
  return (
    <span className="last-updated" title={value}>
      Updated {formatRelativeTime(value)}
    </span>
  )
}

export function SkeletonRows({ rows = 4 }: { rows?: number }) {
  return (
    <div className="skeleton-list" aria-hidden="true">
      {Array.from({ length: rows }, (_, index) => (
        <div className="skeleton-row" key={index}>
          <span />
          <span />
          <span />
        </div>
      ))}
    </div>
  )
}
