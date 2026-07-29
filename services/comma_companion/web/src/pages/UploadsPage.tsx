import { useCallback, useEffect, useState } from 'react'
import {
  Activity,
  ArrowDownToLine,
  Ban,
  CirclePause,
  DatabaseZap,
  FileSearch,
  Gauge,
  RotateCcw,
  Search,
  ServerCog,
} from 'lucide-react'
import { Link } from 'react-router'
import { api } from '../api/client'
import type { CommandReceipt, Device, Job, Upload, UploadState } from '../api/types'
import { UploadRow } from '../components/RecordRows'
import { EmptyState, ErrorState, LoadingState, Metric, PageHeader, Panel, ProgressBar, StatusBadge } from '../components/ui'
import { useApi } from '../hooks/useApi'
import { useVisibilityPolling } from '../hooks/useVisibilityPolling'
import { formatBitrate, formatBytes, formatLocalDate } from '../utils'

type UploadFilter = '' | Extract<UploadState, 'uploading' | 'failed' | 'canceled' | 'ready'>

const filters: Array<{ value: UploadFilter; label: string }> = [
  { value: '', label: 'All' },
  { value: 'uploading', label: 'Transferring' },
  { value: 'failed', label: 'Failed' },
  { value: 'canceled', label: 'Canceled' },
  { value: 'ready', label: 'Transfer complete' },
]

const terminalCommandStates = new Set<CommandReceipt['state']>([
  'succeeded',
  'failed',
  'rejected',
  'expired',
  'canceled',
])

export const ACTIVE_UPLOAD_POLL_INTERVAL_MS = 1_000

export default function UploadsPage() {
  const pageSize = 100
  const [filter, setFilter] = useState<UploadFilter>('')
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [uploadOffset, setUploadOffset] = useState(0)
  const [jobOffset, setJobOffset] = useState(0)
  useEffect(() => {
    const timeout = window.setTimeout(() => {
      setDebouncedSearch(search.trim())
      setUploadOffset(0)
      setJobOffset(0)
    }, 250)
    return () => window.clearTimeout(timeout)
  }, [search])
  const state = useApi(
    () => api.uploadPage(
      filter || undefined,
      pageSize,
      uploadOffset,
      debouncedSearch || undefined,
    ),
    [filter, uploadOffset, debouncedSearch],
  )
  const snapshotState = useApi(() => api.uploadSnapshot(), [])
  const devicesState = useApi(() => api.devices(), [])
  const jobsState = useApi(
    () => api.jobs({
      q: debouncedSearch || undefined,
      limit: pageSize,
      offset: jobOffset,
    }),
    [jobOffset, debouncedSearch],
  )
  const jobCountsState = useApi(() => api.jobCounts(), [])
  useVisibilityPolling(
    state.refresh,
    state.failureCount,
    ACTIVE_UPLOAD_POLL_INTERVAL_MS,
  )
  useVisibilityPolling(snapshotState.refresh, snapshotState.failureCount, 3_000)
  useVisibilityPolling(devicesState.refresh, devicesState.failureCount, 5_000)
  useVisibilityPolling(jobsState.refresh, jobsState.failureCount, 3_000)
  useVisibilityPolling(jobCountsState.refresh, jobCountsState.failureCount, 3_000)
  const uploads = state.data?.items ?? []
  const devicesById = new Map(
    (devicesState.data ?? []).map((device) => [device.id, device]),
  )
  const jobs = jobsState.data?.items ?? []
  const snapshot = snapshotState.data
  const jobCounts = jobCountsState.data ?? {}
  const activeBps = snapshot?.bytes_per_second_60s ?? 0
  const queued = snapshot?.pending_bytes ?? 0
  const processing = (jobCounts.queued ?? 0) + (jobCounts.leased ?? 0) + (jobCounts.running ?? 0)
  const failures = (snapshot?.failed_uploads ?? 0) + (jobCounts.failed ?? 0)
  const jobsTotal = Object.keys(jobCounts).length
    ? Object.values(jobCounts).reduce((sum, count) => sum + count, 0)
    : jobsState.data?.total ?? jobs.length
  const uploadFilterCount = (value: UploadFilter): number | undefined => {
    if (debouncedSearch) return value === filter ? state.data?.total : undefined
    if (value === filter) return state.data?.total
    if (!snapshot || !value) return value ? undefined : state.data?.total
    if (value === 'failed') return snapshot.failed_uploads
    if (value === 'ready') return snapshot.completed_uploads
    return undefined
  }
  const refreshAll = useCallback(() => {
    void Promise.all([
      state.refresh(),
      snapshotState.refresh(),
      jobsState.refresh(),
      jobCountsState.refresh(),
      devicesState.refresh(),
    ])
  }, [
    state.refresh,
    snapshotState.refresh,
    jobsState.refresh,
    jobCountsState.refresh,
    devicesState.refresh,
  ])

  return (
    <>
      <PageHeader
        eyebrow="Ingest pipeline"
        title="Uploads"
        description="Two distinct lanes: durable transfer from the comma, then verification and server-side processing."
      />

      <div className="metric-grid metric-grid-four">
        <Metric label="Ingress" value={formatBitrate(activeBps)} detail={`${snapshot?.active_uploads ?? 0} active transfer`} tone={activeBps > 0 ? 'info' : undefined} icon={<ArrowDownToLine size={18} />} />
        <Metric label="Pending transfer" value={formatBytes(queued)} detail="not yet received by server" icon={<DatabaseZap size={18} />} />
        <Metric label="Server jobs" value={processing} detail={`${jobsTotal} total recorded`} tone={processing ? 'warn' : undefined} icon={<ServerCog size={18} />} />
        <Metric label="Failures" value={failures} detail="transfer and worker failures" tone={failures ? 'bad' : 'good'} icon={<Activity size={18} />} />
      </div>

      <Panel className="upload-control-panel">
        <div className="toolbar">
          <div className="filter-tabs" role="tablist" aria-label="Upload state">
            {filters.map((item) => (
              <button
                type="button"
                role="tab"
                aria-selected={filter === item.value}
                className={filter === item.value ? 'filter-active' : ''}
                onClick={() => {
                  setFilter(item.value)
                  setUploadOffset(0)
                }}
                key={item.value || 'all'}
              >
                {item.label}
                {uploadFilterCount(item.value) != null && (
                  <span>{uploadFilterCount(item.value)?.toLocaleString()}</span>
                )}
              </button>
            ))}
          </div>
          <label className="search-field">
            <Search size={16} />
            <input
              value={search}
              onChange={(event) => {
                setSearch(event.target.value)
                setUploadOffset(0)
                setJobOffset(0)
              }}
              maxLength={256}
              placeholder="File, route, or device"
            />
          </label>
        </div>
      </Panel>

      <div className="pipeline-headers">
        <div><Gauge size={16} /><span>Device transfer</span><strong>resumable chunks</strong></div>
        <i />
        <div><FileSearch size={16} /><span>Integrity</span><strong>size + SHA-256</strong></div>
        <i />
        <div><ServerCog size={16} /><span>Derived data</span><strong>AV1 + telemetry</strong></div>
      </div>

      <Panel className="upload-table-panel">
        {state.loading && !state.data ? (
          <LoadingState label="Reading upload queue" />
        ) : state.error && !state.data ? (
          <ErrorState error={state.error} retry={state.refresh} />
        ) : uploads.length ? (
          <div className="upload-list upload-list-detailed">
            {uploads.map((upload) => (
              <div className="managed-upload-row" key={upload.id}>
                <UploadRow upload={upload} expanded />
                <DeviceUploadActions
                  upload={upload}
                  device={devicesById.get(upload.device_id)}
                  deviceStateLoading={devicesState.loading && !devicesState.data}
                  deviceStateError={devicesState.error}
                  onChanged={refreshAll}
                />
              </div>
            ))}
          </div>
        ) : (
          <EmptyState
            title={search || filter ? 'No uploads match' : 'Upload queue is empty'}
            description={search || filter ? 'Adjust the search or state filter.' : 'The archive is fully caught up.'}
            icon={<CirclePause />}
          />
        )}
        {state.data && state.data.total > 0 && (
          <PageControls
            offset={state.data.offset}
            itemCount={state.data.items.length}
            total={state.data.total}
            limit={state.data.limit}
            loading={state.loading}
            onOffset={setUploadOffset}
            label="uploads"
          />
        )}
      </Panel>

      <Panel
        className="upload-table-panel section-gap"
        kicker="Server-side work"
        title="Real worker jobs"
      >
        {jobsState.loading && !jobsState.data ? (
          <LoadingState label="Reading worker jobs" />
        ) : jobsState.error && !jobsState.data ? (
          <ErrorState error={jobsState.error} retry={jobsState.refresh} />
        ) : jobs.length ? (
          <div className="server-job-list">
            {jobs.map((job) => <ServerJobRow job={job} onChanged={refreshAll} key={job.id} />)}
          </div>
        ) : (
          <EmptyState
            title={search ? 'No worker jobs match' : 'No worker jobs recorded'}
            description={search ? 'Adjust the shared search field.' : 'Verification, telemetry, and AV1 jobs will appear here.'}
            icon={<ServerCog />}
          />
        )}
        {jobsState.data && jobsState.data.total > 0 && (
          <PageControls
            offset={jobOffset}
            itemCount={jobsState.data.items.length}
            total={jobsState.data.total}
            limit={pageSize}
            loading={jobsState.loading}
            onOffset={setJobOffset}
            label="jobs"
          />
        )}
      </Panel>
    </>
  )
}

function DeviceUploadActions({
  upload,
  device,
  deviceStateLoading,
  deviceStateError,
  onChanged,
}: {
  upload: Upload
  device?: Device
  deviceStateLoading: boolean
  deviceStateError?: Error
  onChanged: () => void
}) {
  const deviceManaged = Boolean(upload.file_id) || upload.source_kind === 'device'
  const operation =
    deviceManaged
      ? upload.state === 'uploading'
        ? 'cancel_upload'
        : upload.state === 'failed'
          ? 'retry_upload'
          : undefined
      : undefined
  const [confirmingCancel, setConfirmingCancel] = useState(false)
  const [sending, setSending] = useState(false)
  const [issuedOperation, setIssuedOperation] = useState<typeof operation>()
  const [receipt, setReceipt] = useState<CommandReceipt>()
  const [actionError, setActionError] = useState<Error>()
  const commandPending = receipt != null && !terminalCommandStates.has(receipt.state)

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
        setActionError(new Error('Command status polling exceeded the command expiry window.'))
        return
      }
      try {
        const latest = await api.commandStatus(upload.device_id, receipt.id)
        if (disposed) return
        failures = 0
        setReceipt(latest)
        if (terminalCommandStates.has(latest.state)) {
          onChanged()
          return
        }
      } catch (error) {
        if (disposed) return
        failures += 1
        setActionError(error instanceof Error ? error : new Error('Command status is unavailable'))
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
  }, [onChanged, receipt?.expires_at, receipt?.id, receipt?.state, upload.device_id])

  if (!operation) return null

  const capability = `command_${operation}`
  const disabledReason = deviceStateLoading
    ? 'Loading device capabilities.'
    : deviceStateError && !device
      ? `Device state is unavailable: ${deviceStateError.message}`
      : !device
        ? 'This upload device is not available.'
        : !device.online
          ? 'The device is offline.'
          : !device.capabilities?.includes(capability)
            ? `The agent did not advertise ${capability}; this action is unavailable.`
            : undefined
  const busy = sending || commandPending
  const label = operation === 'cancel_upload' ? 'Cancel transfer' : 'Retry transfer'
  const buttonLabel = sending
    ? operation === 'cancel_upload' ? 'Canceling' : 'Queuing retry'
    : commandPending
      ? operation === 'cancel_upload' ? 'Cancel pending' : 'Retry pending'
      : label

  const sendCommand = async () => {
    if (disabledReason || busy) return
    setSending(true)
    setConfirmingCancel(false)
    setIssuedOperation(operation)
    setReceipt(undefined)
    setActionError(undefined)
    try {
      const latest = await api.command(upload.device_id, {
        type: operation,
        args: upload.file_id ? { file_id: upload.file_id } : { upload_id: upload.id },
        expires_in_seconds: 3_600,
      })
      setReceipt(latest)
      if (terminalCommandStates.has(latest.state)) onChanged()
    } catch (error) {
      setActionError(error instanceof Error ? error : new Error(`${label} failed`))
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="upload-device-actions">
      <div className="upload-device-control-copy">
        <strong>Device command</strong>
        <span>
          {operation === 'cancel_upload'
            ? 'Stops this server session; protected spool data stays on the comma.'
            : 'Queues retained spool or source data under a fresh upload session.'}
        </span>
      </div>
      {confirmingCancel ? (
        <div className="upload-cancel-confirm" role="alert">
          <span>Cancel this transfer? The comma retains its protected spool copy.</span>
          <button
            type="button"
            className="button button-ghost"
            disabled={sending}
            onClick={() => setConfirmingCancel(false)}
          >
            Keep transferring
          </button>
          <button
            type="button"
            className="button button-danger"
            disabled={Boolean(disabledReason) || busy}
            title={disabledReason}
            onClick={() => void sendCommand()}
          >
            Confirm cancel
          </button>
        </div>
      ) : (
        <button
          type="button"
          className="button button-ghost"
          aria-label={`${label} ${upload.filename}`}
          disabled={Boolean(disabledReason) || busy}
          title={disabledReason}
          onClick={() => {
            if (operation === 'cancel_upload') setConfirmingCancel(true)
            else void sendCommand()
          }}
        >
          {operation === 'cancel_upload' ? <Ban size={14} /> : <RotateCcw size={14} />}
          {buttonLabel}
        </button>
      )}
      {disabledReason && <span className="upload-control-reason">{disabledReason}</span>}
      {receipt && (
        <div className="upload-command-result" title={receipt.id}>
          <StatusBadge
            state={commandStatusTone(receipt.state)}
            label={`${(issuedOperation ?? operation) === 'cancel_upload' ? 'cancel' : 'retry'} ${receipt.state}`}
            pulse={commandPending}
          />
          {(receipt.error ?? receipt.message) && <span>{receipt.error ?? receipt.message}</span>}
        </div>
      )}
      {actionError && <div className="row-error">{actionError.message}</div>}
    </div>
  )
}

function commandStatusTone(state: CommandReceipt['state']): string {
  if (state === 'succeeded') return 'healthy'
  if (state === 'failed' || state === 'rejected' || state === 'expired') return 'error'
  if (state === 'canceled') return 'idle'
  if (state === 'queued') return 'warning'
  return 'running'
}

function PageControls({
  offset,
  itemCount,
  total,
  limit,
  loading,
  onOffset,
  label,
}: {
  offset: number
  itemCount: number
  total: number
  limit: number
  loading: boolean
  onOffset: (offset: number) => void
  label: string
}) {
  const currentPage = Math.floor(offset / limit) + 1
  const pageCount = Math.max(1, Math.ceil(total / limit))
  return (
    <nav className="catalog-pagination upload-pagination" aria-label={`${label} pages`}>
      <span>
        Showing {offset + 1}–{Math.min(offset + itemCount, total)} of {total.toLocaleString()}
      </span>
      <div>
        <button
          type="button"
          className="button button-secondary"
          disabled={offset === 0 || loading}
          onClick={() => onOffset(Math.max(0, offset - limit))}
        >
          Previous
        </button>
        <strong>Page {currentPage} of {pageCount}</strong>
        <button
          type="button"
          className="button button-secondary"
          disabled={offset + itemCount >= total || loading}
          onClick={() => onOffset(offset + limit)}
        >
          Next
        </button>
      </div>
    </nav>
  )
}

function ServerJobRow({ job, onChanged }: { job: Job; onChanged: () => void }) {
  const [action, setAction] = useState<'cancel' | 'retry'>()
  const [actionError, setActionError] = useState<Error>()
  const terminalHealthy = job.state === 'succeeded'
  const state =
    terminalHealthy
      ? 'healthy'
      : job.state === 'failed'
        ? 'error'
        : job.state === 'canceled'
          ? 'idle'
          : job.state === 'queued'
            ? 'warning'
            : 'running'
  const controlJob = async (operation: 'cancel' | 'retry') => {
    setAction(operation)
    setActionError(undefined)
    try {
      if (operation === 'cancel') await api.cancelJob(job.id)
      else await api.retryJob(job.id)
      onChanged()
    } catch (error) {
      setActionError(error instanceof Error ? error : new Error(`Could not ${operation} job`))
    } finally {
      setAction(undefined)
    }
  }
  const delayed =
    job.state === 'queued' &&
    job.available_at != null &&
    new Date(job.available_at).valueOf() > Date.now()
  return (
    <article className="server-job-row">
      <div>
        <strong>{job.label}</strong>
        <span className="mono" title={job.id}>{job.id}</span>
        <span className="job-associations">
          {job.drive_id && <Link to={`/drives/${encodeURIComponent(job.drive_id)}`}>drive {job.drive_id}</Link>}
          {job.upload_id && <span title={job.upload_id}>upload {job.upload_id}</span>}
          {job.artifact_id && <span title={job.artifact_id}>artifact {job.artifact_id}</span>}
          {job.retry_of_job_id && <span title={job.retry_of_job_id}>retry of {job.retry_of_job_id}</span>}
        </span>
      </div>
      <div>
        <span>Attempts</span>
        <strong>{job.attempts} / {job.max_attempts}</strong>
      </div>
      <div>
        <span>Updated</span>
        <strong>{formatLocalDate(job.updated_at)}</strong>
      </div>
      <div className="server-job-progress">
        <span>{job.progress.toFixed(0)}%</span>
        <ProgressBar value={job.progress} tone={job.state === 'failed' ? 'error' : job.state === 'queued' ? 'warn' : 'primary'} />
      </div>
      <StatusBadge state={state} label={job.state} pulse={job.state === 'leased' || job.state === 'running'} />
      <div className="server-job-actions">
        {['queued', 'leased', 'running'].includes(job.state) && (
          <button
            type="button"
            className="button button-ghost"
            disabled={action != null || job.cancel_requested_at != null}
            onClick={() => void controlJob('cancel')}
          >
            <Ban size={14} />
            {job.cancel_requested_at ? 'Canceling' : action === 'cancel' ? 'Canceling' : 'Cancel'}
          </button>
        )}
        {job.state === 'failed' && job.retryable && (
          <button
            type="button"
            className="button button-ghost"
            disabled={action != null}
            onClick={() => void controlJob('retry')}
          >
            <RotateCcw size={14} />
            {action === 'retry' ? 'Queuing' : 'Retry'}
          </button>
        )}
      </div>
      {delayed && <div className="row-note">Retry scheduled for {formatLocalDate(job.available_at as string)}</div>}
      {job.error && <div className="row-error">{job.error}</div>}
      {actionError && <div className="row-error">{actionError.message}</div>}
    </article>
  )
}
