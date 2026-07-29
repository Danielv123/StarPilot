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

export function DriveRow({ drive, compact = false }: { drive: Drive; compact?: boolean }) {
  const completeness = percent(drive.ready_segments, drive.segment_count)
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
          <div>
            <span>Backup</span>
            <strong>{Math.round(completeness)}%</strong>
          </div>
          <ProgressBar value={completeness} tone={drive.readiness === 'partial' ? 'warn' : 'primary'} />
        </div>
      )}
      <div className="drive-media">
        <Film size={14} />
        <span>{drive.cameras.filter((camera) => camera.available).length} cam</span>
        <FileCode2 size={14} />
        <span>{drive.telemetry_ready ? 'rlog' : 'no rlog'}</span>
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
        label={drive.readiness}
      />
      <ArrowRight className="row-arrow" size={17} />
    </Link>
  )
}
