import {
  BatteryCharging,
  CarFront,
  ChevronRight,
  Cpu,
  Gauge,
  HardDrive,
  Network,
  Radio,
  ShieldCheck,
  Thermometer,
  Wifi,
  WifiOff,
} from 'lucide-react'
import { Link } from 'react-router'
import { api } from '../api/client'
import { EmptyState, ErrorState, LoadingState, Metric, PageHeader, Panel, ProgressBar, StatusBadge } from '../components/ui'
import { useApi } from '../hooks/useApi'
import { useVisibilityPolling } from '../hooks/useVisibilityPolling'
import { formatBitrate, formatBytes, formatLocalDate, formatRelativeTime, percent } from '../utils'

export default function DevicesPage() {
  const state = useApi(() => api.devices(), [])
  useVisibilityPolling(state.refresh, state.failureCount, 5_000)
  if (state.loading && !state.data) return <LoadingState label="Loading enrolled devices" />
  if (state.error && !state.data) return <ErrorState error={state.error} retry={state.refresh} />
  const devices = state.data ?? []

  return (
    <>
      <PageHeader
        eyebrow="Control plane"
        title="Devices"
        description="Live state and constrained remote controls for enrolled comma devices."
      />
      {!devices.length ? (
        <Panel>
          <EmptyState
            title="No devices enrolled"
            description="Install the archive agent and enroll its device token to begin."
            icon={<CarFront />}
          />
        </Panel>
      ) : (
        <div className="device-card-grid">
          {devices.map((device) => (
            <Link className="device-card" to={`/devices/${encodeURIComponent(device.id)}`} key={device.id}>
              <div className="device-card-top">
                <div className={`device-hero-icon ${device.online ? 'device-online' : ''}`}>
                  <CarFront />
                </div>
                <div>
                  <strong>{device.name}</strong>
                  <span>{device.vehicle ?? device.dongle_id ?? device.id}</span>
                </div>
                <StatusBadge
                  state={!device.online ? 'offline' : device.onroad ? 'running' : device.state}
                  label={!device.online ? 'offline' : device.onroad ? 'onroad' : device.offroad ? 'parked' : 'state unknown'}
                  pulse={device.onroad}
                />
              </div>
              <div className="device-card-body">
                <div>
                  {device.online ? <Wifi size={16} /> : <WifiOff size={16} />}
                  <span>Last seen</span>
                  <strong>{formatRelativeTime(device.last_seen_at)}</strong>
                </div>
                <div>
                  <HardDrive size={16} />
                  <span>Upload queue</span>
                  <strong>{formatBytes(device.queue_bytes)}</strong>
                </div>
                <div>
                  <Radio size={16} />
                  <span>Transfer</span>
                  <strong>{formatBitrate(device.upload_bps)}</strong>
                </div>
                <div>
                  <Thermometer size={16} />
                  <span>Device</span>
                  <strong>{device.temperature_c != null ? `${device.temperature_c.toFixed(1)} °C` : '—'}</strong>
                </div>
              </div>
              <div className="device-card-footer">
                <span className="mono">{device.git_commit ?? 'commit unknown'}</span>
                <span>Inspect & control <ChevronRight size={14} /></span>
              </div>
            </Link>
          ))}
        </div>
      )}

      <div className="metric-grid metric-grid-three section-gap">
        <Metric
          label="Enrolled"
          value={devices.length}
          detail={`${devices.filter((device) => device.online).length} reachable`}
          icon={<ShieldCheck size={18} />}
        />
        <Metric
          label="Combined queue"
          value={formatBytes(devices.reduce((sum, device) => sum + (device.queue_bytes ?? 0), 0))}
          detail="durably protected on-device"
          icon={<HardDrive size={18} />}
        />
        <Metric
          label="Current ingress"
          value={formatBitrate(devices.reduce((sum, device) => sum + (device.upload_bps ?? 0), 0))}
          detail="all connected devices"
          icon={<Network size={18} />}
        />
      </div>

      <Panel kicker="Enrollment" title="Archive agent boundary">
        <div className="boundary-grid">
          <div>
            <Cpu />
            <strong>Independent service</strong>
            <p>The agent runs outside the StarPilot checkout and survives normal upstream code swaps.</p>
          </div>
          <div>
            <Gauge />
            <strong>Resource aware</strong>
            <p>Uploads pause on metered networks and expose their protected spool pressure here.</p>
          </div>
          <div>
            <ShieldCheck />
            <strong>Typed controls only</strong>
            <p>No arbitrary shell or generic parameter writes are exposed to this interface.</p>
          </div>
        </div>
      </Panel>
    </>
  )
}

export function DeviceStorage({ used, capacity }: { used?: number; capacity?: number }) {
  const value = percent(used, capacity)
  return (
    <div className="device-storage">
      <div><span>Protected spool</span><strong>{formatBytes(used)} / {formatBytes(capacity)}</strong></div>
      <ProgressBar value={value} tone={value > 85 ? 'error' : value > 65 ? 'warn' : 'primary'} />
    </div>
  )
}

export function DeviceMeta({ label, value }: { label: string; value?: string }) {
  return (
    <div className="detail-row">
      <span>{label}</span>
      <strong title={value}>{value ?? '—'}</strong>
    </div>
  )
}

export function DeviceSeen({ value }: { value?: string }) {
  return (
    <span title={value}>
      {formatLocalDate(value)} · {formatRelativeTime(value)}
    </span>
  )
}
