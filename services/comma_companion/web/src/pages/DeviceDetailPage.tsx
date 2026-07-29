import { useEffect, useState } from 'react'
import {
  AlertTriangle,
  BatteryCharging,
  ChevronLeft,
  CirclePause,
  CirclePlay,
  Code2,
  HardDrive,
  Network,
  Power,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  Thermometer,
  Wifi,
} from 'lucide-react'
import { Link, useParams } from 'react-router'
import { api } from '../api/client'
import type { Command, CommandReceipt } from '../api/types'
import { ErrorState, LoadingState, Metric, PageHeader, Panel, StatusBadge } from '../components/ui'
import { useApi } from '../hooks/useApi'
import { useVisibilityPolling } from '../hooks/useVisibilityPolling'
import { formatBitrate, formatBytes, formatLocalDate } from '../utils'
import { DeviceMeta, DeviceSeen, DeviceStorage } from './DevicesPage'

const safeControls: Array<{
  type: Command['type']
  label: string
  description: string
  icon: typeof CirclePause
  capability: string
  dangerous?: boolean
}> = [
  { type: 'rescan', capability: 'command_rescan', label: 'Rescan archive', description: 'Reconcile completed files against the upload journal.', icon: RefreshCw },
  { type: 'pause', capability: 'command_pause', label: 'Pause uploads', description: 'Keep collecting locally but stop network transfer.', icon: CirclePause },
  { type: 'resume', capability: 'command_resume', label: 'Resume uploads', description: 'Continue the durable upload queue.', icon: CirclePlay },
  { type: 'restart_agent', capability: 'command_restart_agent', label: 'Restart archive agent', description: 'Restart only the independent upload and control service.', icon: RotateCcw },
  { type: 'restart_starpilot', capability: 'command_restart_starpilot', label: 'Restart StarPilot', description: 'Restart the driving stack after on-device safety checks.', icon: RotateCcw, dangerous: true },
  { type: 'reboot_device', capability: 'command_reboot_device', label: 'Reboot device', description: 'Restart the comma after local safety checks.', icon: RotateCcw, dangerous: true },
  { type: 'shutdown_device', capability: 'command_shutdown_device', label: 'Power off', description: 'Shut down the comma while the vehicle is offroad.', icon: Power, dangerous: true },
]

const terminalCommandStates = new Set<CommandReceipt['state']>([
  'succeeded',
  'failed',
  'rejected',
  'expired',
  'canceled',
])

export default function DeviceDetailPage() {
  const { deviceId = '' } = useParams()
  const state = useApi(() => api.device(deviceId), [deviceId])
  const commandHistory = useApi(() => api.commands(deviceId, 8), [deviceId])
  useVisibilityPolling(state.refresh, state.failureCount, 3_000)
  useVisibilityPolling(commandHistory.refresh, commandHistory.failureCount, 5_000)
  const [command, setCommand] = useState<(typeof safeControls)[number]>()
  const [reason, setReason] = useState('')
  const [confirmUsername, setConfirmUsername] = useState('')
  const [password, setPassword] = useState('')
  const [sending, setSending] = useState(false)
  const [receipt, setReceipt] = useState<CommandReceipt>()
  const [commandError, setCommandError] = useState<Error>()

  const closeCommand = () => {
    setCommand(undefined)
    setConfirmUsername('')
    setPassword('')
    setReason('')
  }

  useEffect(() => {
    if (!receipt?.id || terminalCommandStates.has(receipt.state)) return
    let disposed = false
    let timer: number | undefined
    let failures = 0
    const deadline = receipt.expires_at
      ? new Date(receipt.expires_at).valueOf() + 30_000
      : Date.now() + 3_630_000
    const clear = () => {
      if (timer != null) window.clearTimeout(timer)
      timer = undefined
    }
    const schedule = (delay = Math.min(30_000, 1_000 * (2 ** failures))) => {
      clear()
      if (disposed || document.visibilityState !== 'visible') return
      timer = window.setTimeout(() => void poll(), delay)
    }
    const poll = async () => {
      if (disposed) return
      if (Date.now() > deadline) {
        setCommandError(new Error('Command status polling exceeded the command expiry window.'))
        return
      }
      try {
        const latest = await api.commandStatus(deviceId, receipt.id)
        if (disposed) return
        failures = 0
        setReceipt(latest)
        if (terminalCommandStates.has(latest.state)) {
          void state.refresh()
          void commandHistory.refresh()
          return
        }
      } catch (error) {
        if (disposed) return
        failures += 1
        setCommandError(error instanceof Error ? error : new Error('Command status is unavailable'))
      }
      schedule()
    }
    const visibilityChanged = () => {
      if (document.visibilityState === 'visible') schedule(0)
      else clear()
    }
    document.addEventListener('visibilitychange', visibilityChanged)
    schedule(0)
    return () => {
      disposed = true
      clear()
      document.removeEventListener('visibilitychange', visibilityChanged)
    }
  }, [
    commandHistory.refresh,
    deviceId,
    receipt?.expires_at,
    receipt?.id,
    receipt?.state,
    state.refresh,
  ])

  if (state.loading && !state.data) return <LoadingState label="Reading device state" />
  if (state.error && !state.data) return <ErrorState error={state.error} retry={state.refresh} />
  const device = state.data
  if (!device) return null
  const receiptNoticeClass =
    receipt?.state === 'failed' ||
    receipt?.state === 'rejected' ||
    receipt?.state === 'expired'
      ? 'notice-error'
      : receipt?.state === 'succeeded'
        ? 'notice-success'
        : receipt?.state === 'canceled'
          ? 'notice-warning'
          : 'notice-running'

  const submitCommand = async () => {
    if (!command) return
    setSending(true)
    setCommandError(undefined)
    try {
      const powerCommand = command.type === 'reboot_device' || command.type === 'shutdown_device'
      if (command.dangerous) await api.confirmPassword(confirmUsername, password)
      const latest = await api.command(device.id, {
        type: command.type,
        args: powerCommand ? { reason } : {},
        expires_in_seconds: 120,
      })
      setReceipt(latest)
      void commandHistory.refresh()
      closeCommand()
    } catch (error) {
      setCommandError(error instanceof Error ? error : new Error('Command failed'))
    } finally {
      setPassword('')
      setSending(false)
    }
  }

  return (
    <>
      <Link className="back-link" to="/devices"><ChevronLeft size={15} /> Devices</Link>
      <PageHeader
        eyebrow={device.dongle_id ? `Dongle ${device.dongle_id}` : 'Enrolled device'}
        title={device.name}
        description={device.vehicle ?? 'Vehicle identity unavailable'}
        actions={
          <div className="device-live-title">
            <StatusBadge
              state={!device.online ? 'offline' : device.onroad ? 'running' : device.state}
              label={!device.online ? 'offline' : device.onroad ? 'onroad' : device.offroad ? 'parked / offroad' : 'road state unknown'}
              pulse={device.onroad}
            />
            <button className="icon-button" onClick={() => void state.refresh()} title="Refresh device">
              <RefreshCw size={17} />
            </button>
          </div>
        }
      />

      {receipt && (
        <div className={`notice ${receiptNoticeClass}`}>
          Command {receipt.state}: {receipt.error ?? receipt.message ?? receipt.id}
        </div>
      )}
      {commandError && <div className="notice notice-error">{commandError.message}</div>}

      <div className="metric-grid metric-grid-five">
        <Metric label="Network" value={device.network_type ?? '—'} detail={device.ip_address} icon={<Wifi size={18} />} />
        <Metric label="Transfer" value={formatBitrate(device.upload_bps)} detail={`${formatBytes(device.queue_bytes)} queued`} icon={<Network size={18} />} />
        <Metric label="Free space" value={formatBytes(device.free_space_bytes)} detail="on device" icon={<HardDrive size={18} />} />
        <Metric label="Thermal" value={device.temperature_c != null ? `${device.temperature_c.toFixed(1)} °C` : '—'} detail="device temperature" tone={(device.temperature_c ?? 0) > 70 ? 'warn' : undefined} icon={<Thermometer size={18} />} />
        <Metric label="Battery" value={device.battery_percent != null ? `${device.battery_percent}%` : '—'} detail="reported level" icon={<BatteryCharging size={18} />} />
      </div>

      <div className="detail-grid">
        <Panel className="detail-primary" kicker="Live device" title="State & identity">
          <div className="detail-list">
            <div className="detail-row"><span>Last heartbeat</span><strong><DeviceSeen value={device.last_seen_at} /></strong></div>
            <DeviceMeta label="Software" value={device.software_version} />
            <DeviceMeta label="Branch" value={device.git_branch} />
            <DeviceMeta label="Commit" value={device.git_commit} />
            <DeviceMeta label="Device ID" value={device.id} />
            <DeviceMeta label="Capabilities" value={device.capabilities?.join(', ')} />
          </div>
          <DeviceStorage used={device.spool_bytes} capacity={device.spool_capacity_bytes} />
        </Panel>

        <Panel className="detail-secondary" kicker="Remote" title="Constrained controls">
          <div className="control-list">
            {safeControls.map((control) => {
              const Icon = control.icon
              const supported = device.capabilities?.includes(control.capability) === true
              const disabledReason = !device.online
                ? 'Device is offline.'
                : !supported
                  ? `The agent did not advertise ${control.capability}; this action is unavailable.`
                  : control.dangerous && device.offroad !== true
                    ? 'A fresh offroad state is required.'
                    : undefined
              return (
                <button
                  type="button"
                  className={`control-row ${control.dangerous ? 'control-danger' : ''}`}
                  key={control.type}
                  disabled={Boolean(disabledReason)}
                  title={disabledReason}
                  onClick={() => {
                    setCommand(control)
                    setCommandError(undefined)
                  }}
                >
                  <span><Icon size={18} /></span>
                  <div><strong>{control.label}</strong><small>{disabledReason ?? control.description}</small></div>
                </button>
              )
            })}
          </div>
          <div className="safety-note">
            <ShieldAlert size={17} />
            <p>Power actions require fresh authentication and are rejected by both server and device unless the car is offroad.</p>
          </div>
        </Panel>
      </div>

      <Panel
        className="section-gap"
        kicker="Audit trail"
        title="Recent commands"
        action={(
          <button
            type="button"
            className="icon-button"
            aria-label="Refresh command history"
            title="Refresh command history"
            onClick={() => void commandHistory.refresh()}
          >
            <RefreshCw size={16} />
          </button>
        )}
      >
        {commandHistory.loading && !commandHistory.data ? (
          <div className="command-history-state" role="status">Reading recent commands...</div>
        ) : commandHistory.error && !commandHistory.data ? (
          <div className="command-history-state command-history-error" role="alert">
            <span>{commandHistory.error.message}</span>
            <button type="button" className="button button-ghost" onClick={() => void commandHistory.refresh()}>
              Try again
            </button>
          </div>
        ) : commandHistory.data?.length ? (
          <div className="command-history-list">
            {commandHistory.data.map((item) => (
              <article className="command-history-row" key={item.id}>
                <div className="command-history-identity">
                  <strong>{formatCommandLabel(item.type)}</strong>
                  <span className="mono" title={item.id}>{item.id}</span>
                </div>
                <time dateTime={item.issued_at}>{formatLocalDate(item.issued_at)}</time>
                <StatusBadge
                  state={commandStatusTone(item.state)}
                  label={item.state}
                  pulse={!terminalCommandStates.has(item.state)}
                />
                <span
                  className={item.error ? 'command-history-message command-history-message-error' : 'command-history-message'}
                  title={item.error ?? item.message}
                >
                  {item.error ?? item.message ?? 'No result message'}
                </span>
              </article>
            ))}
          </div>
        ) : (
          <div className="command-history-state">No commands have been issued to this device.</div>
        )}
      </Panel>

      <Panel kicker="Boundary" title="What this interface cannot do">
        <div className="boundary-inline">
          <Code2 size={19} />
          <p>No arbitrary shell commands, SSH sessions, or unrestricted parameter writes are exposed. Every action is typed, audited, and revalidated on-device.</p>
        </div>
      </Panel>

      {command && (
        <div className="modal-layer" role="presentation" onMouseDown={(event) => {
          if (event.currentTarget === event.target) closeCommand()
        }}>
          <div className="modal" role="dialog" aria-modal="true" aria-labelledby="command-title">
            <div className={`modal-icon ${command.dangerous ? 'modal-icon-danger' : ''}`}>
              {command.dangerous ? <AlertTriangle /> : <command.icon />}
            </div>
            <h2 id="command-title">{command.label}?</h2>
            <p>{command.description} The request and device response will be written to the audit trail.</p>
            {(command.type === 'reboot_device' || command.type === 'shutdown_device') && (
              <label className="field">
                <span>Reason</span>
                <input value={reason} onChange={(event) => setReason(event.target.value)} placeholder="Required maintenance or safety reason" />
              </label>
            )}
            {command.dangerous && (
              <>
                <label className="field">
                  <span>Confirm username</span>
                  <input autoFocus value={confirmUsername} onChange={(event) => setConfirmUsername(event.target.value)} autoComplete="username" />
                </label>
                <label className="field">
                  <span>Confirm with password</span>
                  <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" />
                </label>
              </>
            )}
            <div className="modal-actions">
              <button type="button" className="button button-ghost" onClick={closeCommand}>Cancel</button>
              <button
                type="button"
                className={`button ${command.dangerous ? 'button-danger' : 'button-primary'}`}
                disabled={
                  sending ||
                  (command.dangerous && (!confirmUsername || !password)) ||
                  ((command.type === 'reboot_device' || command.type === 'shutdown_device') && !reason.trim())
                }
                onClick={() => void submitCommand()}
              >
                {sending ? 'Queuing…' : command.label}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  )
}

function commandStatusTone(state: CommandReceipt['state']): string {
  if (state === 'succeeded') return 'healthy'
  if (state === 'failed' || state === 'rejected' || state === 'expired') return 'error'
  if (state === 'canceled') return 'idle'
  if (state === 'queued') return 'warning'
  return 'running'
}

function formatCommandLabel(type?: string): string {
  if (!type) return 'Command'
  const label = type.replaceAll('_', ' ')
  return `${label.charAt(0).toUpperCase()}${label.slice(1)}`
}
