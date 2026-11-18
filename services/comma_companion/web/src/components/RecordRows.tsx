import {
  ArrowRight,
  Camera,
  Check,
  CircleAlert,
  Clock3,
  FileCode2,
  Film,
  MapPin,
  RadioTower,
} from 'lucide-react'
import { Link } from 'react-router'
import type { Drive, Upload } from '../api/types'
import {
  formatBitrate,
  formatBytes,
  formatDistance,
  formatDurationUs,
  formatLocalDate,
  percent,
} from '../utils'
import { ProgressBar, StatusBadge } from './ui'

const fileIcons: Record<string, typeof Film> = {
  road_camera: Film,
  wide_camera: Camera,
  driver_camera: Camera,
  rlog: FileCode2,
  qlog: FileCode2,
}

export function UploadRow({ upload, expanded = false }: { upload: Upload; expanded?: boolean }) {
  const Icon = fileIcons[upload.kind] ?? RadioTower
  const progress = percent(upload.received_bytes, upload.size_bytes)
  return (
    <article className={`upload-row ${expanded ? 'upload-row-expanded' : ''}`}>
      <div className="file-icon">
        <Icon size={19} />
      </div>
      <div className="upload-identity">
        <strong>{upload.filename}</strong>
        <span>
          {upload.device_name ?? upload.device_id}
          {upload.segment != null ? ` · segment ${upload.segment}` : ''}
        </span>
      </div>
      <div className="upload-progress">
        <div className="upload-progress-label">
          <span>{formatBytes(upload.received_bytes)} / {formatBytes(upload.size_bytes)}</span>
          <span>{Math.round(progress)}%</span>
        </div>
        <ProgressBar value={progress} tone={upload.state === 'failed' ? 'error' : upload.state === 'processing' ? 'warn' : 'primary'} />
      </div>
      <div className="upload-rate">
        <strong>{upload.state === 'uploading' ? formatBitrate(upload.upload_bps) : upload.state.replaceAll('_', ' ')}</strong>
        <span>{upload.eta_seconds != null ? `${upload.eta_seconds}s remaining` : upload.kind.replaceAll('_', ' ')}</span>
      </div>
      <StatusBadge
        state={
          upload.state === 'ready'
            ? 'healthy'
            : upload.state === 'failed'
              ? 'error'
              : upload.state === 'canceled'
                ? 'idle'
                : upload.state === 'paused'
                  ? 'warning'
                  : 'running'
        }
        label={upload.state}
        pulse={upload.state === 'uploading' || upload.state === 'processing'}
      />
      {expanded && upload.jobs && (
        <div className="job-rail">
          {upload.jobs.map((job, index) => (
            <div className={`job-step job-${job.state}`} key={`${upload.id}-${job.label}`}>
              <span>
                {job.state === 'healthy' ? <Check size={12} /> : index + 1}
              </span>
              <div>
                <strong>{job.label}</strong>
                <small>{job.progress != null ? `${job.progress}%` : job.state}</small>
              </div>
            </div>
          ))}
        </div>
      )}
      {expanded && upload.error && (
        <div className="row-error">
          <CircleAlert size={15} />
          {upload.error}
        </div>
      )}
    </article>
  )
}

export interface DriveProgressSummary {
  backupPercent: number
  backupDetail: string
  processingPercent: number
  processingDetail: string
  processingComplete: boolean
}

export function driveProgressSummary(drive: Drive): DriveProgressSummary {
  const backupPercent = percent(
    drive.backup_bytes_received,
    drive.backup_bytes_expected,
  )
  const expectedMedia = Math.max(0, drive.expected_media)
  const readyMedia = Math.min(expectedMedia, Math.max(0, drive.ready_media))
  const prunedMedia = Math.min(
    expectedMedia,
    readyMedia,
    Math.max(0, drive.pruned_media),
  )
  const processingSteps = expectedMedia * (
    drive.raw_video_pruning_required ? 2 : 1
  )
  const completedSteps = readyMedia + (
    drive.raw_video_pruning_required ? prunedMedia : 0
  )
  const processingPercent = percent(completedSteps, processingSteps)
  const processingComplete = processingSteps > 0 && completedSteps >= processingSteps
  return {
    backupPercent,
    backupDetail: drive.backup_bytes_expected > 0
      ? `${formatBytes(drive.backup_bytes_received)} / ${formatBytes(drive.backup_bytes_expected)}`
      : 'No upload record',
    processingPercent,
    processingDetail: expectedMedia > 0
      ? drive.raw_video_pruning_required
        ? `${readyMedia}/${expectedMedia} AV1 · ${prunedMedia}/${expectedMedia} pruned`
        : `${readyMedia}/${expectedMedia} AV1`
      : 'Waiting for camera files',
    processingComplete,
  }
}

export function driveStatusLabel(drive: Drive): string {
  if (drive.readiness !== 'processing') return drive.readiness
  const progress = driveProgressSummary(drive)
  if (progress.processingComplete && !drive.telemetry_ready) {
    return telemetryStatusLabel(drive.telemetry_status)
  }
  if (progress.processingComplete) return 'finalizing'
  return 'processing'
}

export function telemetryStatusLabel(status: Drive['telemetry_status']): string {
  switch (status) {
    case 'awaiting_rlogs':
      return 'awaiting rlogs'
    case 'awaiting_inventory':
      return 'awaiting route inventory'
    case 'extracting':
      return 'extracting telemetry'
    case 'refreshing':
      return 'refreshing telemetry'
    case 'finalizing':
      return 'finalizing telemetry'
    case 'ready':
      return 'telemetry ready'
  }
}

export function rlogBackupLabel(drive: Drive): string {
  if (drive.expected_rlogs > 0) {
    return `${drive.archived_rlogs}/${drive.expected_rlogs} rlogs`
  }
  if (drive.archived_rlogs > 0) {
    return `${drive.archived_rlogs} ${drive.archived_rlogs === 1 ? 'rlog' : 'rlogs'}`
  }
  return 'no rlogs yet'
}

export function DriveProgressBars({
  drive,
  className = '',
}: {
  drive: Drive
  className?: string
}) {
  const progress = driveProgressSummary(drive)
  return (
    <div className={`drive-progress-stack ${className}`.trim()}>
      <div className="drive-progress-item" title={progress.backupDetail}>
        <div>
          <span>Backup</span>
          <strong>{Math.round(progress.backupPercent)}%</strong>
        </div>
        <ProgressBar
          value={progress.backupPercent}
          tone={drive.readiness === 'failed' ? 'error' : 'primary'}
        />
      </div>
      <div className="drive-progress-item" title={progress.processingDetail}>
        <div>
          <span>{progress.processingComplete ? 'Processed' : 'Processing'}</span>
          <strong>{Math.round(progress.processingPercent)}%</strong>
        </div>
        <ProgressBar
          value={progress.processingPercent}
          tone={
            drive.failed_media > 0
              ? 'error'
              : progress.processingComplete
                ? 'primary'
                : 'warn'
          }
        />
      </div>
    </div>
  )
}

export function DriveRow({ drive, compact = false }: { drive: Drive; compact?: boolean }) {
  const statusLabel = driveStatusLabel(drive)
  return (
    <Link to={`/drives/${encodeURIComponent(drive.id)}`} className={`drive-row ${compact ? 'drive-row-compact' : ''}`}>
      <div className="drive-date">
        <strong>{formatLocalDate(drive.started_at)}</strong>
        <span className="mono">{drive.route_name}</span>
      </div>
      <div className="drive-route">
        <MapPin size={15} />
        <div>
          <strong>{drive.location_start ?? 'Location unavailable'}</strong>
          <span>{drive.location_end ? `to ${drive.location_end}` : 'destination unavailable'}</span>
        </div>
      </div>
      <div className="drive-stat">
        <Clock3 size={14} />
        <div>
          <strong>{formatDurationUs(drive.duration_us)}</strong>
          <span>{formatDistance(drive.distance_m)}</span>
        </div>
      </div>
      {!compact && (
        <div className="drive-completeness">
          <DriveProgressBars drive={drive} />
        </div>
      )}
      <div className="drive-media">
        <Film size={14} />
        <span>{drive.cameras.filter((camera) => camera.available).length} cam</span>
        <FileCode2 size={14} />
        <span>{rlogBackupLabel(drive)}</span>
      </div>
      <StatusBadge
        state={
          drive.readiness === 'ready'
            ? 'healthy'
            : drive.readiness === 'failed'
              ? 'error'
              : drive.readiness === 'partial'
                ? 'warning'
                : 'running'
        }
        label={statusLabel}
      />
      <ArrowRight className="row-arrow" size={17} />
    </Link>
  )
}
