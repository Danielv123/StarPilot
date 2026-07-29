import {
  Archive,
  ArrowRight,
  CarFront,
  Database,
  HardDriveUpload,
  RadioTower,
  ServerCog,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { api } from '../api/client'
import type { InboundStatus } from '../api/types'
import { DriveRow, UploadRow } from '../components/RecordRows'
import {
  ErrorState,
  LastUpdated,
  LoadingState,
  Metric,
  PageHeader,
  Panel,
  ProgressBar,
  ServiceIcon,
  StatusBadge,
} from '../components/ui'
import { useApi } from '../hooks/useApi'
import { useVisibilityPolling } from '../hooks/useVisibilityPolling'
import { formatBitrate, formatBytes, formatEta, percent } from '../utils'

const SPEED_HISTORY_LENGTH = 60
export const ACTIVE_PIPELINE_POLL_INTERVAL_MS = 1_000

export function instantaneousSpeed(
  previous: InboundStatus | undefined,
  current: InboundStatus,
): number {
  if (!previous) return current.upload_bps
  const elapsedSeconds = (
    Date.parse(current.generated_at) - Date.parse(previous.generated_at)
  ) / 1_000
  if (
    !Number.isFinite(elapsedSeconds) ||
    elapsedSeconds <= 0 ||
    elapsedSeconds > 10 ||
    current.bytes_received < previous.bytes_received
  ) {
    return current.upload_bps
  }
  return (current.bytes_received - previous.bytes_received) / elapsedSeconds
}

export function sparklinePath(values: number[]): string {
  if (!values.length) return ''
  const maximum = Math.max(1, ...values)
  return values.map((value, index) => {
    const x = values.length === 1 ? 100 : (index / (values.length - 1)) * 100
    const y = 34 - (Math.max(0, value) / maximum) * 30
    return `${index ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)}`
  }).join(' ')
}

function SpeedSparkline({ values }: { values: number[] }) {
  const line = sparklinePath(values)
  if (!line) return null
  return (
    <svg className="metric-sparkline" viewBox="0 0 100 36" preserveAspectRatio="none">
      <path className="metric-sparkline-area" d={`${line} L100,36 L0,36 Z`} />
      <path className="metric-sparkline-line" d={line} />
    </svg>
  )
}

export default function OverviewPage() {
  const state = useApi(() => api.overview(), [])
  const inboundState = useApi(() => api.inbound(), [])
  const activeUploadsState = useApi(() => api.activeUploads(), [])
  const previousInbound = useRef<InboundStatus | undefined>(undefined)
  const [speedHistory, setSpeedHistory] = useState<number[]>([])
  useVisibilityPolling(state.refresh, state.failureCount, 30_000)
  useVisibilityPolling(inboundState.refresh, inboundState.failureCount, 1_000)
  useVisibilityPolling(
    activeUploadsState.refresh,
    activeUploadsState.failureCount,
    ACTIVE_PIPELINE_POLL_INTERVAL_MS,
  )

  useEffect(() => {
    const inbound = inboundState.data
    if (!inbound) return
    const speed = instantaneousSpeed(previousInbound.current, inbound)
    previousInbound.current = inbound
    setSpeedHistory((history) => [...history.slice(1 - SPEED_HISTORY_LENGTH), speed])
  }, [inboundState.data])

  if (state.loading && !state.data) return <LoadingState label="Loading archive overview" />
  if (state.error && !state.data) return <ErrorState error={state.error} retry={state.refresh} />
  const overview = state.data
  if (!overview) return null
  const inbound = inboundState.data
  const activeUploads = activeUploadsState.data ?? overview.active_uploads
  const uploadBps = inbound?.upload_bps ?? overview.upload_bps
  const pendingUploadBytes = inbound?.pending_upload_bytes ?? overview.pending_upload_bytes
  const etaSeconds = uploadBps > 0 ? pendingUploadBytes / uploadBps : undefined
  const devicesOnline = inbound?.devices_online ?? overview.devices_online
  const devicesTotal = inbound?.devices_total ?? overview.devices_total
  const hasStorageCapacity = Boolean(overview.storage.capacity_bytes && overview.storage.capacity_bytes > 0)
  const storagePercent = hasStorageCapacity
    ? percent(overview.storage.used_bytes, overview.storage.capacity_bytes)
    : 0

  return (
    <>
      <PageHeader
        eyebrow="Fleet snapshot"
        title="Archive control"
        description="Durable uploads, private playback, and model replay from one operational view."
        actions={
          <div className="header-status">
            <span className="live-indicator"><i /> LIVE</span>
            <LastUpdated value={inbound?.generated_at ?? overview.generated_at} />
          </div>
        }
      />

      <div className="metric-grid metric-grid-overview">
        <Metric
          label="Comma"
          value={`${devicesOnline} online`}
          detail={`${devicesTotal} enrolled · parked`}
          tone={devicesOnline === devicesTotal ? 'good' : 'warn'}
          icon={<CarFront size={18} />}
        />
        <Metric
          label="Inbound"
          value={formatBitrate(uploadBps)}
          detail={`${formatBytes(pendingUploadBytes)} queued · ETA ${formatEta(etaSeconds)}`}
          tone={uploadBps > 0 ? 'info' : undefined}
          icon={<HardDriveUpload size={18} />}
          background={<SpeedSparkline values={speedHistory} />}
        />
        <Metric
          label="Drive archive"
          value={overview.drives_total.toLocaleString()}
          detail={`${overview.drives_ready.toLocaleString()} playable · ${overview.drives_by_readiness.processing.toLocaleString()} processing`}
          icon={<Archive size={18} />}
        />
        <Metric
          label="Archive storage"
          value={formatBytes(overview.storage.used_bytes)}
          detail={hasStorageCapacity ? `${storagePercent.toFixed(2)}% of ${formatBytes(overview.storage.capacity_bytes)}` : 'host capacity not reported'}
          tone={storagePercent > 90 ? 'bad' : storagePercent > 75 ? 'warn' : undefined}
          icon={<Database size={18} />}
        />
      </div>

      <section className="service-strip" aria-label="Service health">
        {overview.services.map((service) => (
          <div className="service-item" key={service.id}>
            <ServiceIcon state={service.state} />
            <div>
              <strong>{service.label}</strong>
              <span>{service.detail}</span>
            </div>
            <StatusBadge state={service.state} />
          </div>
        ))}
      </section>

      <div className="dashboard-grid">
        <Panel
          className="dashboard-main"
          kicker="Transport"
          title="Active pipeline"
          action={
            <Link className="text-link" to="/uploads">
              All uploads <ArrowRight size={14} />
            </Link>
          }
        >
          <div className="pipeline-summary">
            <div>
              <RadioTower size={18} />
              <span>Device transfer</span>
              <strong>{activeUploads.filter((item) => item.state === 'uploading').length} active</strong>
            </div>
            <i />
            <div>
              <ServerCog size={18} />
              <span>Server processing</span>
              <strong>{overview.jobs_active} active</strong>
            </div>
          </div>
          <div className="upload-list">
            {activeUploads.length ? (
              activeUploads.slice(0, 4).map((upload) => <UploadRow upload={upload} key={upload.id} />)
            ) : (
              <div className="inline-empty">Nothing is transferring. The archive is caught up.</div>
            )}
          </div>
        </Panel>

        <Panel className="dashboard-side" kicker="Capacity" title="Storage composition">
          <div className={`storage-dial ${hasStorageCapacity ? '' : 'storage-dial-unknown'}`} style={{ '--used': `${Math.max(0.8, storagePercent)}%` } as React.CSSProperties}>
            <div>
              <strong>{hasStorageCapacity ? `${storagePercent.toFixed(2)}%` : formatBytes(overview.storage.used_bytes)}</strong>
              <span>{hasStorageCapacity ? 'used' : 'cataloged'}</span>
            </div>
          </div>
          <div className="storage-legend">
            <div>
              <i className="legend-raw" />
              <span>Immutable raw</span>
              <strong>{formatBytes(overview.storage.raw_bytes)}</strong>
            </div>
            <div>
              <i className="legend-derived" />
              <span>AV1 + telemetry</span>
              <strong>{formatBytes(overview.storage.derived_bytes)}</strong>
            </div>
            {hasStorageCapacity && (
              <div>
                <i className="legend-free" />
                <span>Available</span>
                <strong>{formatBytes(
                  overview.storage.free_bytes ??
                  (overview.storage.capacity_bytes as number) - overview.storage.used_bytes,
                )}</strong>
              </div>
            )}
          </div>
          {hasStorageCapacity && <ProgressBar value={storagePercent} />}
          <p className="panel-note">
            {formatBytes(overview.storage.cataloged_bytes)} cataloged. Raw artifacts remain immutable; playback uses the server-side AV1 derivatives.
          </p>
        </Panel>
      </div>

      <Panel
        kicker="Latest"
        title="Recent drives"
        action={
          <Link className="text-link" to="/drives">
            Browse archive <ArrowRight size={14} />
          </Link>
        }
      >
        <div className="drive-list">
          {overview.recent_drives.slice(0, 4).map((drive) => <DriveRow drive={drive} compact key={drive.id} />)}
        </div>
      </Panel>
    </>
  )
}
