import {
  Archive,
  ArrowRight,
  CarFront,
  Database,
  HardDriveUpload,
  RadioTower,
  ServerCog,
} from 'lucide-react'
import { Link } from 'react-router'
import { api } from '../api/client'
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
import { formatBitrate, formatBytes, percent } from '../utils'

export default function OverviewPage() {
  const state = useApi(() => api.overview(), [])
  useVisibilityPolling(state.refresh, state.failureCount, 5_000)

  if (state.loading && !state.data) return <LoadingState label="Loading archive overview" />
  if (state.error && !state.data) return <ErrorState error={state.error} retry={state.refresh} />
  const overview = state.data
  if (!overview) return null
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
            <LastUpdated value={overview.generated_at} />
          </div>
        }
      />

      <div className="metric-grid metric-grid-overview">
        <Metric
          label="Comma"
          value={`${overview.devices_online} online`}
          detail={`${overview.devices_total} enrolled · parked`}
          tone={overview.devices_online === overview.devices_total ? 'good' : 'warn'}
          icon={<CarFront size={18} />}
        />
        <Metric
          label="Inbound"
          value={formatBitrate(overview.upload_bps)}
          detail={`${formatBytes(overview.pending_upload_bytes)} pending`}
          tone={overview.upload_bps > 0 ? 'info' : undefined}
          icon={<HardDriveUpload size={18} />}
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
              <strong>{overview.active_uploads.filter((item) => item.state === 'uploading').length} active</strong>
            </div>
            <i />
            <div>
              <ServerCog size={18} />
              <span>Server processing</span>
              <strong>{overview.jobs_active} active</strong>
            </div>
          </div>
          <div className="upload-list">
            {overview.active_uploads.length ? (
              overview.active_uploads.slice(0, 4).map((upload) => <UploadRow upload={upload} key={upload.id} />)
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
