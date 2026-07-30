import { useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle,
  Archive,
  Camera,
  CheckCircle2,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  Clock3,
  FileCode2,
  FastForward,
  Film,
  Gauge,
  LoaderCircle,
  Maximize2,
  Pause,
  Play,
  Rewind,
  RotateCcw,
  SlidersHorizontal,
  Sparkles,
  TriangleAlert,
} from 'lucide-react'
import { Link, useParams, useSearchParams } from 'react-router'
import { ApiError, api, demoMode } from '../api/client'
import type {
  DriveDetail,
  DriveSeries,
  MediaSyncIndex,
  ModelSummary,
  ParameterSchema,
  SignalSeries,
  SimulationRequest,
  SimulationProgress,
  SimulationResult,
} from '../api/types'
import { EventTimeline, SignalChart } from '../components/Charts'
import { rlogBackupLabel, telemetryStatusLabel } from '../components/RecordRows'
import {
  EmptyState,
  ErrorState,
  LoadingState,
  PageHeader,
  Panel,
  ProgressBar,
  StatusBadge,
} from '../components/ui'
import { useApi } from '../hooks/useApi'
import { useVisibilityPolling } from '../hooks/useVisibilityPolling'
import {
  frameAtVideoPts,
  frameNearestDriveTime,
  mediaManifestEqual,
  mediaSyncUrlPins,
  playbackTimeForAvailableMedia,
  sortedPlayableMedia,
  validateMediaSyncIndex,
} from '../mediaSync'
import {
  clamp,
  formatDurationUs,
  formatLocalDate,
  mergeQuery,
  metricImprovement,
  percent,
} from '../utils'

const defaultSignalIds = [
  'lateral.desired_acceleration',
  'lateral.actual_acceleration',
  'control.applied_torque',
  'vehicle.steering_angle',
  'vehicle.speed',
]

const signalAliases: Record<string, string[]> = {
  'lateral.desired_acceleration': ['lateral.desired_acceleration', 'desired_lateral_accel'],
  'lateral.actual_acceleration': ['lateral.actual_acceleration', 'actual_lateral_accel'],
  'control.applied_torque': ['control.applied_torque', 'applied_torque'],
  'vehicle.steering_angle': ['vehicle.steering_angle', 'steering_angle'],
  'vehicle.speed': ['vehicle.speed', 'v_ego'],
}

function findSignal(series: DriveSeries | undefined, canonicalId: string): SignalSeries | undefined {
  const aliases = signalAliases[canonicalId] ?? [canonicalId]
  const signal = series?.signals.find((item) => aliases.includes(item.id))
  if (!signal) return undefined
  if (canonicalId === 'vehicle.steering_angle' && (signal.unit === 'rad' || signal.id === canonicalId)) {
    return {
      ...signal,
      id: canonicalId,
      label: 'Steering angle',
      unit: 'deg',
      points: signal.points.map((point) => ({
        ...point,
        value: point.value == null ? null : point.value * (180 / Math.PI),
      })),
    }
  }
  return signal
}

export function valueAt(
  signal: SignalSeries | undefined,
  tUs: number,
  gaps: DriveSeries['gaps'] = [],
  maximumAgeUs = 100_000,
): number | null {
  if (!signal?.points.length) return null
  let result = signal.points[0]
  for (const point of signal.points) {
    if (Math.abs(point.t_us - tUs) < Math.abs(result.t_us - tUs)) result = point
  }
  if (Math.abs(result.t_us - tUs) > maximumAgeUs) return null
  const intervalStart = Math.min(result.t_us, tUs)
  const intervalEnd = Math.max(result.t_us, tUs)
  if (gaps.some((gap) =>
    gap.kind === 'telemetry_gap' &&
    gap.start_us <= intervalEnd &&
    gap.end_us >= intervalStart)) {
    return null
  }
  return result.value
}

function seriesOrEmpty(
  series: SignalSeries | undefined,
  gaps: DriveSeries['gaps'] = [],
): SignalSeries {
  if (!series) return { id: 'missing', label: 'Unavailable', unit: '', points: [] }
  const telemetryGaps = gaps.filter((gap) => gap.kind === 'telemetry_gap')
  if (!telemetryGaps.length) return series
  const retained = series.points.filter((point) =>
    !telemetryGaps.some((gap) => point.t_us >= gap.start_us && point.t_us <= gap.end_us))
  const gapBoundaries = telemetryGaps.flatMap((gap) => [
    { t_us: gap.start_us, value: null },
    { t_us: gap.end_us, value: null },
  ])
  return {
    ...series,
    points: [...retained, ...gapBoundaries].sort((left, right) => left.t_us - right.t_us),
  }
}

function numberValue(value: number | null, digits = 2): string {
  return value == null || !Number.isFinite(value) ? '—' : value.toFixed(digits)
}

export interface PlaybackSyncStatus {
  mode: 'exact' | 'approximate' | 'pending' | 'unavailable'
  label: string
  reason?: string
  segmentNumber?: number
}

export function DriveMedia({
  driveId,
  camera,
  durationUs,
  playheadUs,
  onPlayhead,
  onSyncStatus,
  telemetrySha256,
  timelineVersion,
  refreshWhileProcessing = false,
}: {
  driveId: string
  camera: string
  durationUs: number
  playheadUs: number
  onPlayhead: (value: number, updateVideo?: boolean) => void
  onSyncStatus?: (status: PlaybackSyncStatus) => void
  telemetrySha256?: string
  timelineVersion?: string
  refreshWhileProcessing?: boolean
}) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const preloadRef = useRef<HTMLVideoElement>(null)
  const resumeAfterSwitch = useRef(false)
  const videoReportedDriveUs = useRef<number | undefined>(undefined)
  const onPlayheadRef = useRef(onPlayhead)
  onPlayheadRef.current = onPlayhead
  const [playing, setPlaying] = useState(false)
  const [videoError, setVideoError] = useState<string>()
  const [syncRecord, setSyncRecord] = useState<{
    artifactId: string
    index: MediaSyncIndex
  }>()
  const [syncLoading, setSyncLoading] = useState(false)
  const [syncError, setSyncError] = useState<string>()
  const manifestState = useApi(
    () => api.mediaManifest(driveId, camera),
    [driveId, camera],
    mediaManifestEqual,
  )
  const wasRefreshingManifest = useRef(refreshWhileProcessing)
  useVisibilityPolling(
    manifestState.refresh,
    manifestState.failureCount,
    1_000,
    60_000,
    refreshWhileProcessing,
  )
  useEffect(() => {
    const transitionedToReady = wasRefreshingManifest.current && !refreshWhileProcessing
    wasRefreshingManifest.current = refreshWhileProcessing
    if (!transitionedToReady) return
    let active = true
    // A scheduled manifest request may have started just before the drive's
    // final readiness transaction. Wait for it, then force one post-transition
    // read so an older empty manifest cannot become permanent.
    void manifestState.refresh().finally(() => {
      if (active) void manifestState.refresh()
    })
    return () => { active = false }
  }, [manifestState.refresh, refreshWhileProcessing])
  const manifest =
    manifestState.data?.drive_id === driveId && manifestState.data.camera === camera
      ? manifestState.data
      : undefined
  const playableItems = useMemo(
    () => sortedPlayableMedia(manifest?.items ?? []),
    [manifest],
  )
  const mediaPlayheadUs = playbackTimeForAvailableMedia(playableItems, playheadUs)
  const activeIndex = playableItems.findIndex((item, index) => {
    const startUs = item.start_t_us as number
    const endUs = startUs + (item.duration_us as number)
    return mediaPlayheadUs >= startUs && (
      mediaPlayheadUs < endUs ||
      (index === playableItems.length - 1 && mediaPlayheadUs === endUs)
    )
  })
  const activeItem = activeIndex >= 0 ? playableItems[activeIndex] : undefined
  const nextItem = activeIndex >= 0 ? playableItems[activeIndex + 1] : undefined
  const mediaUrl = activeItem?.url
  const syncPins = useMemo(
    () => activeItem?.sync_url ? mediaSyncUrlPins(activeItem.sync_url) : undefined,
    [activeItem?.sync_url],
  )
  const exactSync =
    syncRecord && syncRecord.artifactId === activeItem?.artifact_id
      ? syncRecord.index
      : undefined

  useEffect(() => {
    if (mediaPlayheadUs === playheadUs) return
    videoReportedDriveUs.current = mediaPlayheadUs
    onPlayheadRef.current(mediaPlayheadUs, false)
  }, [mediaPlayheadUs, playheadUs])

  useEffect(() => {
    let active = true
    setSyncRecord(undefined)
    setSyncError(undefined)
    if (activeItem?.sync_mode !== 'exact' || !activeItem.sync_url) {
      setSyncLoading(false)
      return () => { active = false }
    }
    if (
      !syncPins ||
      !activeItem.video_sha256 ||
      !activeItem.frame_index_artifact_id ||
      !activeItem.timeline_origin ||
      !telemetrySha256 ||
      !timelineVersion ||
      syncPins.videoArtifactId !== activeItem.artifact_id ||
      syncPins.videoSha256 !== activeItem.video_sha256 ||
      syncPins.frameIndexArtifactId !== activeItem.frame_index_artifact_id ||
      syncPins.telemetrySha256 !== telemetrySha256
    ) {
      setSyncLoading(false)
      setSyncError('The exact frame timeline is missing or disagrees with its generation pins. Reload the drive before using exact alignment.')
      return () => { active = false }
    }
    setSyncLoading(true)
    void api.mediaSync(activeItem.sync_url)
      .then((loaded) => {
        if (!active) return
        const validationError = validateMediaSyncIndex(loaded, {
          driveId,
          camera,
          segmentNumber: activeItem.segment_number,
          videoArtifactId: activeItem.artifact_id,
          videoSha256: activeItem.video_sha256!,
          frameIndexArtifactId: activeItem.frame_index_artifact_id!,
          frameIndexSha256: syncPins.frameIndexSha256,
          telemetrySha256,
          timelineVersion,
          timelineOrigin: activeItem.timeline_origin!,
        })
        if (validationError) {
          setSyncError(validationError)
          return
        }
        setSyncRecord({ artifactId: activeItem.artifact_id, index: loaded })
      })
      .catch((error) => {
        if (active) {
          setSyncError(error instanceof Error ? error.message : 'The exact frame timeline could not be loaded.')
        }
      })
      .finally(() => {
        if (active) setSyncLoading(false)
      })
    return () => { active = false }
  }, [
    activeItem?.artifact_id,
    activeItem?.segment_number,
    activeItem?.sync_mode,
    activeItem?.sync_url,
    activeItem?.video_sha256,
    activeItem?.frame_index_artifact_id,
    activeItem?.timeline_origin,
    camera,
    driveId,
    syncPins,
    telemetrySha256,
    timelineVersion,
  ])

  const syncStatus = useMemo<PlaybackSyncStatus>(() => {
    if (manifestState.loading && !manifest) {
      return { mode: 'pending', label: 'CHECKING FRAME SYNC' }
    }
    if (!activeItem) {
      return {
        mode: 'unavailable',
        label: 'NO VIDEO AT PLAYHEAD',
        reason: 'This camera has no archived media covering the selected drive time.',
      }
    }
    if (exactSync) {
      return {
        mode: 'exact',
        label: 'EXACT FRAME SYNC',
        reason: exactSync.timeline_origin === 'stable'
          ? `Verified stable ${exactSync.timeline_version} frame-to-rlog join.`
          : `Verified exact frame mapping for this segment on provisional timeline ${exactSync.timeline_version}; route-relative bookmarks may move as earlier rlogs arrive.`,
        segmentNumber: activeItem.segment_number,
      }
    }
    if (activeItem.sync_mode === 'exact' && syncLoading) {
      return {
        mode: 'pending',
        label: 'VERIFYING FRAME SYNC',
        segmentNumber: activeItem.segment_number,
      }
    }
    return {
      mode: 'approximate',
      label: 'APPROXIMATE ALIGNMENT',
      reason:
        syncError ??
        activeItem.sync_reason ??
        'No verified frame-to-rlog mapping is available; segment start and elapsed video time are being used.',
      segmentNumber: activeItem.segment_number,
    }
  }, [activeItem, exactSync, manifest, manifestState.loading, syncError, syncLoading])

  useEffect(() => {
    onSyncStatus?.(syncStatus)
  }, [onSyncStatus, syncStatus])

  const reportVideoPosition = (
    videoTimeSeconds: number,
    sync: MediaSyncIndex | undefined = exactSync,
  ) => {
    let driveUs: number | undefined
    if (sync) {
      driveUs = frameAtVideoPts(sync, Math.round(videoTimeSeconds * 1_000_000))?.drive_t_us
    } else if (activeItem?.start_t_us != null) {
      driveUs = activeItem.start_t_us + Math.round(videoTimeSeconds * 1_000_000)
    }
    if (driveUs == null) return
    const bounded = clamp(driveUs, 0, durationUs)
    videoReportedDriveUs.current = bounded
    onPlayheadRef.current(bounded, false)
  }

  useEffect(() => {
    const video = videoRef.current
    if (!video || activeItem?.start_t_us == null) return
    if (videoReportedDriveUs.current === playheadUs) {
      videoReportedDriveUs.current = undefined
      return
    }
    if (video.readyState < HTMLMediaElement.HAVE_METADATA) return

    let targetSeconds: number
    if (exactSync) {
      const frame = frameNearestDriveTime(exactSync, playheadUs)
      if (!frame) return
      targetSeconds = frame.pts_us / 1_000_000
      if (frame.drive_t_us !== playheadUs) {
        videoReportedDriveUs.current = frame.drive_t_us
        onPlayheadRef.current(frame.drive_t_us, false)
      }
    } else {
      targetSeconds = (playheadUs - activeItem.start_t_us) / 1_000_000
    }
    if (Math.abs(video.currentTime - targetSeconds) > 0.001) {
      video.currentTime = Math.max(0, targetSeconds)
    }
  }, [activeItem?.artifact_id, activeItem?.start_t_us, exactSync, playheadUs])

  useEffect(() => {
    setVideoError(undefined)
    if (!resumeAfterSwitch.current) setPlaying(false)
  }, [mediaUrl])

  useEffect(() => {
    const video = videoRef.current
    if (!video || !exactSync || typeof video.requestVideoFrameCallback !== 'function') return
    let callbackId: number | undefined
    const trackFrame: VideoFrameRequestCallback = (_now, metadata) => {
      reportVideoPosition(metadata.mediaTime, exactSync)
      callbackId = video.requestVideoFrameCallback(trackFrame)
    }
    callbackId = video.requestVideoFrameCallback(trackFrame)
    return () => {
      if (callbackId != null) video.cancelVideoFrameCallback(callbackId)
    }
  }, [exactSync, mediaUrl])

  const seek = (value: number) => {
    const bounded = clamp(value, 0, durationUs)
    const exactFrame = exactSync && activeItem?.start_t_us != null &&
      bounded >= activeItem.start_t_us &&
      bounded <= activeItem.start_t_us + (activeItem.duration_us ?? 0)
      ? frameNearestDriveTime(exactSync, bounded)
      : undefined
    onPlayheadRef.current(exactFrame?.drive_t_us ?? bounded, false)
  }

  const toggle = async () => {
    const video = videoRef.current
    if (!video) return
    if (video.paused) {
      try {
        await video.play()
      } catch (error) {
        setVideoError(error instanceof Error ? error.message : 'Playback could not start.')
      }
    } else {
      video.pause()
    }
  }

  return (
    <div className="media-player">
      <div className="video-stage">
        {manifestState.loading && !manifest ? (
          <div className="video-unavailable">
            <LoaderCircle className="spin" size={30} />
            <strong>Loading archived media manifest</strong>
            <span>Resolving verified segment assets…</span>
          </div>
        ) : mediaUrl && !videoError ? (
          <>
            <video
              ref={videoRef}
              key={mediaUrl}
              src={mediaUrl}
              preload="auto"
              playsInline
              onLoadedMetadata={(event) => {
                if (exactSync) {
                  const frame = frameNearestDriveTime(exactSync, playheadUs)
                  if (frame) {
                    event.currentTarget.currentTime = frame.pts_us / 1_000_000
                    if (frame.drive_t_us !== playheadUs) {
                      videoReportedDriveUs.current = frame.drive_t_us
                      onPlayheadRef.current(frame.drive_t_us, false)
                    }
                  }
                } else if (activeItem?.start_t_us != null) {
                  event.currentTarget.currentTime = Math.max(
                    0,
                    (playheadUs - activeItem.start_t_us) / 1_000_000,
                  )
                }
                if (resumeAfterSwitch.current) {
                  resumeAfterSwitch.current = false
                  void event.currentTarget.play()
                }
              }}
              onPlay={() => setPlaying(true)}
              onPause={() => setPlaying(false)}
              onTimeUpdate={(event) => {
                if (
                  exactSync &&
                  typeof event.currentTarget.requestVideoFrameCallback === 'function'
                ) return
                reportVideoPosition(event.currentTarget.currentTime)
              }}
              onEnded={() => {
                if (nextItem?.start_t_us != null) {
                  const currentEndUs =
                    (activeItem?.start_t_us ?? 0) + (activeItem?.duration_us ?? 0)
                  const frameDurationUs = activeItem?.fps && activeItem.fps > 0
                    ? Math.round(1_000_000 / activeItem.fps)
                    : 50_000
                  if (
                    nextItem.start_t_us - currentEndUs <=
                    Math.max(1_000, frameDurationUs * 1.5)
                  ) {
                    resumeAfterSwitch.current = true
                    onPlayheadRef.current(nextItem.start_t_us, false)
                    return
                  }
                }
                const lastExactFrame = exactSync?.points.at(-1)
                onPlayheadRef.current(lastExactFrame?.drive_t_us ?? playheadUs, false)
                setPlaying(false)
              }}
              onError={() => setVideoError('The archived AV1 WebM segment is not available for this camera window.')}
            />
            {nextItem?.url && (
              <video
                ref={preloadRef}
                className="media-preload"
                src={nextItem.url}
                preload="metadata"
                muted
                aria-hidden="true"
              />
            )}
          </>
        ) : (
          <div className="video-unavailable">
            <Camera size={36} strokeWidth={1.4} />
            <strong>
              {demoMode
                ? 'Demo mode has no media asset'
                : manifestState.error
                  ? 'Media manifest unavailable'
                  : 'No archived segment at this playhead'}
            </strong>
            <span>{videoError ?? manifestState.error?.message ?? 'Telemetry and model replay remain available.'}</span>
          </div>
        )}
        <div className="backup-source">
          <Archive size={13} /> SERVER BACKUP · AV1 WEBM
          {activeItem && ` · SEG ${activeItem.segment_number}`}
        </div>
        <div
          className={`media-sync-status media-sync-${syncStatus.mode}`}
          title={syncStatus.reason}
        >
          {syncStatus.mode === 'exact' ? <CheckCircle2 size={12} /> : <Clock3 size={12} />}
          {syncStatus.label}
        </div>
        <div className="video-timecode">{formatDurationUs(playheadUs, true)} / {formatDurationUs(durationUs)}</div>
      </div>
      <div className="player-controls">
        <button type="button" className="player-button" aria-label="Back 10 seconds" onClick={() => seek(playheadUs - 10_000_000)}><Rewind size={17} /></button>
        <button type="button" className="player-button player-button-main" aria-label={playing ? 'Pause' : 'Play'} disabled={!mediaUrl || Boolean(videoError)} onClick={() => void toggle()}>
          {playing ? <Pause size={18} fill="currentColor" /> : <Play size={18} fill="currentColor" />}
        </button>
        <button type="button" className="player-button" aria-label="Forward 10 seconds" onClick={() => seek(playheadUs + 10_000_000)}><FastForward size={17} /></button>
        <input
          className="video-scrubber"
          type="range"
          min={0}
          max={Math.max(1, durationUs)}
          value={clamp(playheadUs, 0, durationUs)}
          onChange={(event) => seek(Number(event.target.value))}
          aria-label="Video position"
          style={{ '--progress': `${percent(playheadUs, durationUs)}%` } as React.CSSProperties}
        />
        <button
          type="button"
          className="player-button"
          aria-label="Fullscreen"
          onClick={() => void videoRef.current?.requestFullscreen?.()}
          disabled={!mediaUrl}
        >
          <Maximize2 size={17} />
        </button>
      </div>
    </div>
  )
}

function Inspector({
  series,
  playheadUs,
}: {
  series?: DriveSeries
  playheadUs: number
}) {
  const desired = valueAt(findSignal(series, 'lateral.desired_acceleration'), playheadUs, series?.gaps)
  const actual = valueAt(findSignal(series, 'lateral.actual_acceleration'), playheadUs, series?.gaps)
  const applied = valueAt(findSignal(series, 'control.applied_torque'), playheadUs, series?.gaps)
  const angle = valueAt(findSignal(series, 'vehicle.steering_angle'), playheadUs, series?.gaps)
  const speed = valueAt(findSignal(series, 'vehicle.speed'), playheadUs, series?.gaps)
  const activeEvent = series?.events.find((event) => event.start_us <= playheadUs && event.end_us >= playheadUs)

  return (
    <div className="inspector">
      <div className="inspector-time">
        <div><span>PLAYHEAD</span><strong>{formatDurationUs(playheadUs, true)}</strong></div>
        <StatusBadge
          state={activeEvent?.lane === 'driver_overlay' ? 'warning' : activeEvent?.lane === 'alert' ? 'error' : 'healthy'}
          label={activeEvent?.label ?? 'clean window'}
        />
      </div>
      <div className="inspector-section">
        <h3>Lateral response</h3>
        <div className="inspector-values">
          <div><i className="signal-desired" /><span>Desired accel</span><strong>{numberValue(desired)} <small>m/s²</small></strong></div>
          <div><i className="signal-actual" /><span>Actual accel</span><strong>{numberValue(actual)} <small>m/s²</small></strong></div>
          <div><i className="signal-torque" /><span>Applied torque</span><strong>{numberValue(applied, 3)}</strong></div>
        </div>
      </div>
      <div className="inspector-section">
        <h3>Vehicle state</h3>
        <div className="inspector-values">
          <div><Gauge size={14} /><span>Speed</span><strong>{numberValue(speed)} <small>m/s</small></strong></div>
          <div><RotateCcw size={14} /><span>Steering angle</span><strong>{numberValue(angle, 1)} <small>deg</small></strong></div>
        </div>
      </div>
      <div className="inspector-section inspector-source">
        <h3>Source</h3>
        <p><FileCode2 size={14} /> Indexed rlog at nearest monotonic timestamp. Values are SI units.</p>
      </div>
    </div>
  )
}

function ParameterScope({
  parameter,
}: {
  parameter: ParameterSchema['parameters'][number]
}) {
  const modelOnly = parameter.scope === 'model_only'
  const label = modelOnly
    ? 'OFFLINE MODEL ONLY'
    : parameter.runtime_supported === true
      ? 'RUNTIME-SUPPORTED'
      : parameter.runtime_supported === false
        ? 'NOT ON-DEVICE'
        : undefined
  return label
    ? <em className={`parameter-scope ${modelOnly || parameter.runtime_supported === false ? 'scope-model-only' : ''}`}>{label}</em>
    : null
}

function ParameterField({
  parameter,
  value,
  onChange,
}: {
  parameter: ParameterSchema['parameters'][number]
  value: number | boolean | string
  onChange: (value: number | boolean | string) => void
}) {
  if (parameter.type === 'boolean') {
    return (
      <label className="tune-toggle">
        <span>
          <strong>{parameter.label} <ParameterScope parameter={parameter} /></strong>
          <small>{parameter.description}</small>
        </span>
        <input type="checkbox" checked={Boolean(value)} onChange={(event) => onChange(event.target.checked)} />
      </label>
    )
  }
  if (parameter.type === 'enum') {
    return (
      <label className="field tune-field">
        <span>{parameter.label} <ParameterScope parameter={parameter} /></span>
        <select value={String(value)} onChange={(event) => onChange(event.target.value)}>
          {parameter.options?.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
        </select>
      </label>
    )
  }
  const number = Number(value)
  return (
    <div className="tune-field">
      <div className="tune-field-label">
        <span>
          {parameter.label} {parameter.unit && <em>{parameter.unit}</em>}
          <ParameterScope parameter={parameter} />
        </span>
        <input
          type="number"
          value={number}
          min={parameter.minimum}
          max={parameter.maximum}
          step={parameter.step}
          onChange={(event) => onChange(Number(event.target.value))}
          aria-label={`${parameter.label} exact value`}
        />
      </div>
      {parameter.minimum != null && parameter.maximum != null && (
        <input
          type="range"
          min={parameter.minimum}
          max={parameter.maximum}
          step={parameter.step}
          value={number}
          onChange={(event) => onChange(Number(event.target.value))}
          aria-label={parameter.label}
          style={{ '--progress': `${percent(number - parameter.minimum, parameter.maximum - parameter.minimum)}%` } as React.CSSProperties}
        />
      )}
      {parameter.description && <small>{parameter.description}</small>}
    </div>
  )
}

function simulationSnapshotKey(request: SimulationRequest): string {
  return JSON.stringify({
    ...request,
    parameters: Object.fromEntries(
      Object.entries(request.parameters).sort(([left], [right]) => left.localeCompare(right)),
    ),
  })
}

function errorDetails(error: Error): string[] {
  if (!(error instanceof ApiError)) return []
  const details = error.detail
  if (!details || typeof details !== 'object') return error.code ? [error.code] : []
  const record = details as Record<string, unknown>
  const reasons = Array.isArray(record.reasons) ? record.reasons : []
  const reasonLines = reasons.flatMap((reason) => {
    if (typeof reason === 'string') return [reason]
    if (!reason || typeof reason !== 'object') return []
    const item = reason as Record<string, unknown>
    const code = typeof item.code === 'string' ? item.code.replaceAll('_', ' ') : undefined
    const message = typeof item.message === 'string' ? item.message : undefined
    return [code && message ? `${code}: ${message}` : message ?? code].filter(Boolean) as string[]
  })
  if (reasonLines.length) return reasonLines
  const values = Object.entries(record).flatMap(([key, value]) => {
    if (value == null) return []
    if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
      return [`${key.replaceAll('_', ' ')}: ${String(value)}`]
    }
    return []
  })
  return values.length ? values : error.code ? [error.code] : []
}

function averageSeriesMagnitude(points: Array<{ value: number | null }>): number | undefined {
  const values = points.flatMap((point) =>
    point.value != null && Number.isFinite(point.value) ? [Math.abs(point.value)] : [],
  )
  return values.length
    ? values.reduce((sum, value) => sum + value, 0) / values.length
    : undefined
}

export function TuneWorkbench({
  driveId,
  playheadUs,
  durationUs,
  models,
  modelsError,
  simulationEligible,
  simulationEligibilityReasons,
  telemetrySha256,
  timelineVersion,
}: {
  driveId: string
  playheadUs: number
  durationUs: number
  models?: ModelSummary[]
  modelsError?: Error
  simulationEligible: boolean
  simulationEligibilityReasons: DriveDetail['simulation_eligibility_reasons']
  telemetrySha256?: string
  timelineVersion?: string
}) {
  const eligibleModels = useMemo(
    () => models?.filter((item) => item.enabled && item.eligible) ?? [],
    [models],
  )
  const [modelId, setModelId] = useState('')
  const [schema, setSchema] = useState<ParameterSchema>()
  const [schemaError, setSchemaError] = useState<Error>()
  const [parameters, setParameters] = useState<Record<string, number | boolean | string>>({})
  const [horizonUs, setHorizonUs] = useState(1_000_000)
  const [result, setResult] = useState<SimulationResult>()
  const [running, setRunning] = useState(false)
  const [runError, setRunError] = useState<Error>()
  const [runProgress, setRunProgress] = useState<SimulationProgress>()
  const activeRun = useRef(0)
  const runAbort = useRef<AbortController | undefined>(undefined)

  useEffect(() => {
    if (!eligibleModels.some((item) => item.id === modelId)) {
      setModelId(eligibleModels[0]?.id ?? '')
    }
  }, [eligibleModels, modelId])

  useEffect(() => {
    if (!modelId || !eligibleModels.some((item) => item.id === modelId)) {
      setSchema(undefined)
      setParameters({})
      return
    }
    let active = true
    setSchema(undefined)
    setSchemaError(undefined)
    void api.parameterSchema(modelId)
      .then((loaded) => {
        if (!active) return
        setSchema(loaded)
        setParameters(Object.fromEntries(loaded.parameters.map((parameter) => [parameter.id, parameter.default])))
      })
      .catch((error) => {
        if (active) setSchemaError(error instanceof Error ? error : new Error('Parameter schema unavailable'))
      })
    return () => { active = false }
  }, [eligibleModels, modelId])

  const model = eligibleModels.find((item) => item.id === modelId)
  const historyReady = playheadUs >= (model?.history_us ?? 3_000_000)
  const horizonReady = playheadUs + horizonUs <= durationUs
  const canRun = Boolean(
    simulationEligible &&
    telemetrySha256 &&
    timelineVersion &&
    model &&
    schema &&
    historyReady &&
    horizonReady &&
    !running,
  )
  const requestSnapshot = useMemo<SimulationRequest | undefined>(
    () => model && telemetrySha256 && timelineVersion
      ? {
          drive_id: driveId,
          model_id: model.id,
          start_us: playheadUs,
          horizon_us: horizonUs,
          telemetry_sha256: telemetrySha256,
          timeline_version: timelineVersion,
          parameters,
        }
      : undefined,
    [driveId, horizonUs, model, parameters, playheadUs, telemetrySha256, timelineVersion],
  )
  const requestSnapshotKey = requestSnapshot ? simulationSnapshotKey(requestSnapshot) : ''
  const currentSnapshotKey = useRef(requestSnapshotKey)
  currentSnapshotKey.current = requestSnapshotKey
  const groups = useMemo(
    () => [...new Set(schema?.parameters.map((parameter) => parameter.group) ?? [])],
    [schema],
  )

  useEffect(() => {
    activeRun.current += 1
    runAbort.current?.abort()
    runAbort.current = undefined
    setRunning(false)
    setResult(undefined)
    setRunError(undefined)
    setRunProgress(undefined)
  }, [requestSnapshotKey])

  useEffect(() => () => runAbort.current?.abort(), [])

  const run = async () => {
    if (!requestSnapshot) return
    const runId = activeRun.current + 1
    activeRun.current = runId
    runAbort.current?.abort()
    const controller = new AbortController()
    runAbort.current = controller
    const submittedKey = simulationSnapshotKey(requestSnapshot)
    setRunning(true)
    setResult(undefined)
    setRunError(undefined)
    setRunProgress(undefined)
    try {
      const loaded = await api.simulate(requestSnapshot, {
        signal: controller.signal,
        onProgress: (progress) => {
          if (activeRun.current === runId && currentSnapshotKey.current === submittedKey) {
            setRunProgress(progress)
          }
        },
      })
      if (activeRun.current === runId && currentSnapshotKey.current === submittedKey) {
        setResult(loaded)
      }
    } catch (error) {
      if (activeRun.current === runId && currentSnapshotKey.current === submittedKey) {
        setRunError(error instanceof Error ? error : new Error('Simulation failed'))
      }
    } finally {
      if (activeRun.current === runId) {
        setRunning(false)
        if (runAbort.current === controller) runAbort.current = undefined
      }
    }
  }

  if (modelsError) {
    return <ErrorState error={modelsError} title="Model worker is unavailable" />
  }
  if (!models?.length) {
    return <EmptyState title="No model registered" description="An allow-listed vehicle dynamics model is required for Tune mode." icon={<SlidersHorizontal />} />
  }
  if (!eligibleModels.length) {
    const reasons = [...new Set(models.flatMap((item) => item.eligibility_reasons))]
    return (
      <EmptyState
        title="No model is eligible for counterfactual replay"
        description={[
          'Registered historical models remain visible on the Models page, but cannot be selected or run.',
          ...reasons,
        ].join(' ')}
        icon={<SlidersHorizontal />}
      />
    )
  }

  return (
    <div className="tune-workbench">
      <aside className="tune-sidebar">
        <div className="tune-heading">
          <div><Sparkles size={18} /><span><strong>Counterfactual tune</strong><small>Offline model estimate</small></span></div>
          <StatusBadge state={model?.state ?? 'idle'} label={model?.mode.replaceAll('_', ' ') ?? 'select model'} />
        </div>
        <label className="field">
          <span>Vehicle model</span>
          <select value={modelId} onChange={(event) => setModelId(event.target.value)}>
            {eligibleModels.map((item) => <option value={item.id} key={item.id}>{item.name} · {item.version}</option>)}
          </select>
        </label>
        <div className="tune-window">
          <div><span>Start</span><strong>{formatDurationUs(playheadUs, true)}</strong></div>
          <div>
            <span>Horizon</span>
            <select value={horizonUs} onChange={(event) => setHorizonUs(Number(event.target.value))}>
              <option value={500_000}>0.5 s</option>
              <option value={1_000_000}>1.0 s</option>
              <option value={1_500_000}>1.5 s</option>
              <option value={2_000_000}>2.0 s</option>
            </select>
          </div>
        </div>
        {!historyReady && (
          <div className="tune-warning"><TriangleAlert size={16} /><span>Move at least {formatDurationUs((model?.history_us ?? 3_000_000) - playheadUs, true)} later to provide clean model history.</span></div>
        )}
        {!horizonReady && (
          <div className="tune-warning"><TriangleAlert size={16} /><span>The requested horizon runs beyond the end of the drive.</span></div>
        )}
        {!simulationEligible && simulationEligibilityReasons.map((reason) => (
          <div className="tune-warning" key={reason.code}>
            <TriangleAlert size={16} />
            <span>
              <strong>{reason.code.replaceAll('_', ' ')}</strong>
              {reason.message}
            </span>
          </div>
        ))}
        {(!telemetrySha256 || !timelineVersion) && (
          <div className="tune-warning">
            <TriangleAlert size={16} />
            <span>A pinned telemetry hash and timeline generation are required before a replay can run.</span>
          </div>
        )}
        {schemaError ? (
          <div className="tune-warning"><AlertTriangle size={16} /><span>{schemaError.message}</span></div>
        ) : !schema ? (
          <div className="tune-loading"><LoaderCircle className="spin" /> Loading parameter schema…</div>
        ) : (
          <div className="parameter-groups">
            {groups.map((group) => (
              <details open key={group}>
                <summary>{group}<ChevronDown size={14} /></summary>
                <div>
                  {schema.parameters.filter((parameter) => parameter.group === group).map((parameter) => (
                    <ParameterField
                      key={parameter.id}
                      parameter={parameter}
                      value={parameters[parameter.id] ?? parameter.default}
                      onChange={(value) => setParameters((current) => ({ ...current, [parameter.id]: value }))}
                    />
                  ))}
                </div>
              </details>
            ))}
          </div>
        )}
        <div className="tune-actions">
          <button
            className="button button-primary"
            type="button"
            disabled={!canRun}
            onClick={() => void run()}
          >
            {running
              ? (
                  <>
                    <LoaderCircle className="spin" size={16} />
                    {runProgress?.cancel_requested
                      ? 'Canceling…'
                      : `Simulating${runProgress ? ` ${Math.round(runProgress.progress * 100)}%` : '…'}`}
                  </>
                )
              : <><Sparkles size={16} /> Run candidate</>}
          </button>
          {running && (
            <button
              className="button button-danger"
              type="button"
              disabled={runProgress?.cancel_requested}
              onClick={() => {
                runAbort.current?.abort()
                setRunProgress((current) => current
                  ? { ...current, cancel_requested: true }
                  : current)
              }}
            >
              Cancel run
            </button>
          )}
          <button
            className="button button-ghost"
            type="button"
            disabled={!schema || running}
            onClick={() => {
              if (!schema) return
              setParameters(Object.fromEntries(schema.parameters.map((parameter) => [parameter.id, parameter.default])))
              setResult(undefined)
            }}
          >
            Reset
          </button>
        </div>
        {runProgress && (
          <div className="simulation-job-progress">
            <span>
              Job <strong className="mono">{runProgress.job_id}</strong>
            </span>
            <span>{runProgress.cancel_requested ? 'cancel requested' : runProgress.state.replaceAll('_', ' ')}</span>
            <ProgressBar
              value={runProgress.progress * 100}
              tone={runProgress.cancel_requested ? 'warn' : 'primary'}
            />
          </div>
        )}
        <p className="never-apply">Nothing in Tune mode can apply parameters to the car.</p>
      </aside>

      <section className="tune-results">
        {runError && (
          <div className="notice notice-error simulation-error">
            <strong>{runError.message}</strong>
            {errorDetails(runError).map((detail) => <small key={detail}>{detail}</small>)}
          </div>
        )}
        {!result ? (
          <div className="tune-placeholder">
            <SlidersHorizontal />
            <strong>Replay this moment with a different tune</strong>
            <p>Choose a clean playhead, adjust the schema-driven parameters, and run a bounded candidate simulation.</p>
            <div>
              <span><i className="trace-recorded" /> Recorded actual</span>
              <span><i className="trace-baseline" /> Baseline estimate</span>
              <span><i className="trace-candidate" /> Candidate</span>
            </div>
          </div>
        ) : (
          <>
            <div className="result-header">
              <div><div className="eyebrow">Simulation result</div><h3>{formatDurationUs(result.start_us, true)} → {formatDurationUs(result.end_us, true)}</h3></div>
              <StatusBadge
                state={result.validity.valid ? 'healthy' : 'warning'}
                label={
                  result.validity.valid
                    ? result.exact_baseline ? 'valid exact baseline' : 'valid approximate baseline'
                    : 'blocked by guardrail'
                }
              />
            </div>
            <SignalChart
              title="Desired vs response"
              series={result.traces.map((trace) => ({
                ...trace,
                color:
                  trace.id === 'recorded'
                    ? '#63b3ff'
                    : trace.id === 'baseline'
                      ? '#f4bb62'
                      : trace.id === 'candidate'
                        ? '#86efc2'
                        : '#8d969c',
                muted: trace.id === 'desired',
              }))}
              startUs={result.start_us}
              endUs={result.end_us}
              playheadUs={result.start_us}
              height={240}
            />
            <div className="result-metrics">
              {result.metrics.map((metric) => {
                const improvement = metricImprovement(
                  metric.baseline,
                  metric.candidate,
                  metric.objective,
                )
                const good = improvement >= 0
                return (
                  <div key={metric.label}>
                    <span>{metric.label}</span>
                    <strong>{metric.candidate.toFixed(3)} <small>{metric.unit}</small></strong>
                    <em className={good ? 'metric-delta-good' : 'metric-delta-bad'}>
                      {good ? 'improved' : 'worse'} {Math.abs(improvement).toFixed(1)}%
                      {metric.objective === 'absolute_lower' ? ' by magnitude' : ''} vs baseline
                    </em>
                  </div>
                )
              })}
            </div>
            <div className="result-uncertainty">
              <div>
                <span>Baseline mean σ</span>
                <strong>{numberValue(averageSeriesMagnitude(result.uncertainty.baseline_std) ?? null, 4)} <small>m/s²</small></strong>
              </div>
              <div>
                <span>Candidate mean σ</span>
                <strong>{numberValue(averageSeriesMagnitude(result.uncertainty.candidate_std) ?? null, 4)} <small>m/s²</small></strong>
              </div>
              <div>
                <span>Ensemble disagreement p95</span>
                <strong>{numberValue(result.uncertainty.maximum_disagreement_p95_normalized ?? null, 3)} <small>normalized</small></strong>
              </div>
            </div>
            {result.warnings.map((warning, index) => (
              <div
                className={`tune-warning result-warning warning-${warning.severity}`}
                key={`${warning.code}-${index}`}
              >
                {warning.severity === 'blocker' ? <AlertTriangle size={16} /> : <CircleHelp size={16} />}
                <span>
                  <strong>{warning.code.replaceAll('_', ' ')}</strong>
                  {warning.message}
                  {warning.details && Object.keys(warning.details).length > 0 && (
                    <small>{JSON.stringify(warning.details)}</small>
                  )}
                </span>
              </div>
            ))}
            <details className="provenance">
              <summary>Provenance & validity <ChevronDown size={14} /></summary>
              <div>
                <p><span>Model</span><strong>{result.provenance.model_name}</strong></p>
                <p><span>Model SHA-256</span><strong className="mono">{result.provenance.model_hash}</strong></p>
                <p><span>Controller mode</span><strong>{result.provenance.controller_mode}</strong></p>
                <p><span>StarPilot commit</span><strong className="mono">{result.provenance.starpilot_commit}</strong></p>
                <p><span>Extractor</span><strong>{result.provenance.extractor_version}</strong></p>
                <p><span>Telemetry SHA-256</span><strong className="mono">{result.provenance.telemetry_hash ?? 'not published'}</strong></p>
                <p><span>Timeline generation</span><strong className="mono">{result.provenance.timeline_version ?? 'not published'}</strong></p>
                <p><span>Input alignment</span><strong>{result.provenance.input_alignment ?? 'not published'}</strong></p>
                <p><span>Source rlogs</span><strong className="mono">{result.provenance.rlog_hashes.join(' · ') || 'not published'}</strong></p>
                <p><span>Window flags</span><strong>{result.validity.flags.join(' · ')}</strong></p>
              </div>
            </details>
          </>
        )}
      </section>
    </div>
  )
}

export default function DriveStudioPage() {
  const { driveId = '' } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const detailState = useApi(() => api.drive(driveId), [driveId])
  const modelsState = useApi(() => api.models(), [])
  const [camera, setCamera] = useState('road')
  const [tuneMode, setTuneMode] = useState(searchParams.get('mode') === 'tune')
  const initialUs = Number(searchParams.get('t') ?? 0)
  const [playheadUs, setPlayheadUs] = useState(Number.isFinite(initialUs) ? Math.max(0, initialUs) : 0)
  const [series, setSeries] = useState<DriveSeries>()
  const [seriesLoading, setSeriesLoading] = useState(false)
  const [seriesError, setSeriesError] = useState<Error>()
  const [focusedSeries, setFocusedSeries] = useState<DriveSeries>()
  const [focusedSeriesLoading, setFocusedSeriesLoading] = useState(false)
  const [focusedSeriesError, setFocusedSeriesError] = useState<Error>()
  const [playbackSync, setPlaybackSync] = useState<PlaybackSyncStatus>({
    mode: 'pending',
    label: 'CHECKING FRAME SYNC',
  })
  const detail = detailState.data
  useVisibilityPolling(
    detailState.refresh,
    detailState.failureCount,
    1_000,
    60_000,
    Boolean(detail && detail.readiness !== 'ready'),
  )
  const focusWindow = useMemo(() => {
    const durationUs = detail?.duration_us ?? 0
    if (durationUs <= 0) return { startUs: 0, endUs: 0 }
    const windowUs = Math.min(30_000_000, durationUs)
    const quantizedCenterUs = Math.round(playheadUs / 5_000_000) * 5_000_000
    const startUs = clamp(
      quantizedCenterUs - Math.floor(windowUs / 2),
      0,
      Math.max(0, durationUs - windowUs),
    )
    return { startUs, endUs: startUs + windowUs }
  }, [detail?.duration_us, playheadUs])

  useEffect(() => {
    if (!detail) return
    const telemetrySha256 = detail.telemetry_generation?.ndjson_sha256
    const timelineVersion = detail.telemetry_generation?.timeline_version
    setSeries(undefined)
    if (!telemetrySha256 || !timelineVersion) {
      setSeriesLoading(false)
      setSeriesError(
        detail.telemetry_ready
          ? new Error('Telemetry is missing immutable generation pins; reload after indexing completes.')
          : undefined,
      )
      return
    }
    setSeriesLoading(true)
    setSeriesError(undefined)
    let active = true
    void api.series(
      driveId,
      0,
      detail.duration_us,
      ['vehicle.speed'],
      1_000,
      { telemetry_sha256: telemetrySha256, timeline_version: timelineVersion },
      10_000,
    )
      .then((loaded) => { if (active) setSeries(loaded) })
      .catch((error) => { if (active) setSeriesError(error instanceof Error ? error : new Error('Telemetry unavailable')) })
      .finally(() => { if (active) setSeriesLoading(false) })
    return () => { active = false }
  }, [
    detail?.duration_us,
    detail?.telemetry_generation?.ndjson_sha256,
    detail?.telemetry_generation?.timeline_version,
    driveId,
  ])

  useEffect(() => {
    const telemetrySha256 = detail?.telemetry_generation?.ndjson_sha256
    const timelineVersion = detail?.telemetry_generation?.timeline_version
    setFocusedSeries(undefined)
    setFocusedSeriesError(undefined)
    if (
      !detail ||
      !telemetrySha256 ||
      !timelineVersion ||
      focusWindow.endUs <= focusWindow.startUs
    ) {
      setFocusedSeriesLoading(false)
      return
    }

    let active = true
    setFocusedSeriesLoading(true)
    const timeout = window.setTimeout(() => {
      void api.series(
        driveId,
        focusWindow.startUs,
        focusWindow.endUs,
        defaultSignalIds,
        5_000,
        { telemetry_sha256: telemetrySha256, timeline_version: timelineVersion },
        2_000,
      )
        .then((loaded) => {
          if (active) setFocusedSeries(loaded)
        })
        .catch((error) => {
          if (active) {
            setFocusedSeriesError(
              error instanceof Error ? error : new Error('Focused telemetry unavailable'),
            )
          }
        })
        .finally(() => {
          if (active) setFocusedSeriesLoading(false)
        })
    }, 200)
    return () => {
      active = false
      window.clearTimeout(timeout)
    }
  }, [
    detail?.duration_us,
    detail?.telemetry_generation?.ndjson_sha256,
    detail?.telemetry_generation?.timeline_version,
    driveId,
    focusWindow.endUs,
    focusWindow.startUs,
  ])

  useEffect(() => {
    if (!detail) return
    const bounded = clamp(playheadUs, 0, detail.duration_us)
    const timeout = window.setTimeout(() => {
      const current = searchParams.toString()
      const next = mergeQuery(current, {
        t: Math.round(bounded),
        mode: tuneMode ? 'tune' : null,
        camera,
      })
      const nextValue = next.slice(1)
      if (nextValue !== current) {
        setSearchParams(new URLSearchParams(nextValue), { replace: true })
      }
    }, 180)
    return () => window.clearTimeout(timeout)
  }, [playheadUs, tuneMode, camera, detail, searchParams, setSearchParams])

  useEffect(() => {
    const requested = searchParams.get('camera')
    if (requested) setCamera(requested)
  }, [])

  if (detailState.loading && !detail) return <LoadingState label="Opening Drive Studio" />
  if (detailState.error && !detail) return <ErrorState error={detailState.error} retry={detailState.refresh} title="Drive unavailable" />
  if (!detail) return null

  const seek = (value: number) => setPlayheadUs(clamp(Math.round(value), 0, detail.duration_us))
  const currentSegment = detail.segments.find((segment) => segment.start_us <= playheadUs && segment.end_us >= playheadUs)
  const selectedCameraReadiness = currentSegment?.camera_readiness?.[camera] ?? currentSegment?.state
  const selectedCameraLabel = detail.cameras.find((item) => item.id === camera)?.label ?? camera
  const focusedStart = focusWindow.startUs
  const focusedEnd = focusWindow.endUs
  const actual = seriesOrEmpty(
    findSignal(focusedSeries, 'lateral.actual_acceleration'),
    focusedSeries?.gaps,
  )
  const desired = seriesOrEmpty(
    findSignal(focusedSeries, 'lateral.desired_acceleration'),
    focusedSeries?.gaps,
  )
  const torque = seriesOrEmpty(
    findSignal(focusedSeries, 'control.applied_torque'),
    focusedSeries?.gaps,
  )
  const angle = seriesOrEmpty(
    findSignal(focusedSeries, 'vehicle.steering_angle'),
    focusedSeries?.gaps,
  )
  const speed = seriesOrEmpty(
    findSignal(focusedSeries, 'vehicle.speed'),
    focusedSeries?.gaps,
  )
  const declaredStreamGaps = detail.segments.flatMap((segment) =>
    (segment.missing ?? []).map((missing) => `segment ${segment.index}: ${missing}`),
  )

  return (
    <div className="drive-studio-page">
      <Link className="back-link" to="/drives"><ChevronLeft size={15} /> Drive archive</Link>
      <PageHeader
        eyebrow={detail.route_name}
        title={`${detail.location_start ?? 'Unknown start'} → ${detail.location_end ?? 'Unknown end'}`}
        description={`${formatLocalDate(detail.started_at)} · ${formatDurationUs(detail.duration_us)} · ${detail.vehicle ?? 'Vehicle unavailable'}`}
        actions={
          <div className="studio-mode-switch">
            <button type="button" className={!tuneMode ? 'active' : ''} onClick={() => setTuneMode(false)}><Film size={15} /> Explore</button>
            <button
              type="button"
              className={tuneMode ? 'active' : ''}
              disabled={!detail.simulation_eligible}
              title={
                detail.simulation_eligible
                  ? 'Open counterfactual Tune mode'
                  : detail.simulation_eligibility_reasons.map((reason) => reason.message).join(' ')
              }
              onClick={() => setTuneMode(true)}
            >
              <SlidersHorizontal size={15} /> Tune
            </button>
          </div>
        }
      />
      {!detail.simulation_eligible && (
        <div className="notice notice-warning tune-eligibility-notice">
          <TriangleAlert size={16} />
          <span>
            <strong>Counterfactual Tune mode is unavailable for this drive.</strong>
            {detail.simulation_eligibility_reasons.map((reason) => reason.message).join(' ')}
          </span>
        </div>
      )}
      {detail.route_inventory && (
        detail.route_inventory.state === 'partial' ||
        detail.route_inventory.missing_file_count > 0 ||
        detail.route_inventory.missing_segment_numbers.length > 0
      ) && (
        <div className="notice notice-warning route-inventory-notice">
          <TriangleAlert size={16} />
          <span>
            <strong>Route inventory generation {detail.route_inventory.generation} is partial.</strong>
            {detail.route_inventory.capability_source === 'route_union_unconfigured'
              ? ' Expected stream capabilities were not configured, so completeness remains fail-closed.'
              : ` ${detail.route_inventory.missing_file_count.toLocaleString()} declared files and ${detail.route_inventory.missing_segment_numbers.length.toLocaleString()} segments are missing.`}
            {detail.route_inventory.closure_evidence.length > 0 &&
              ` Closure evidence: ${detail.route_inventory.closure_evidence.join(', ')}.`}
            {declaredStreamGaps.length > 0 && ` ${declaredStreamGaps.slice(0, 6).join(' · ')}`}
          </span>
        </div>
      )}

      <div className="studio-top">
        <div className="studio-media">
          <div className="camera-tabs" role="tablist" aria-label="Camera">
            {detail.cameras.map((item) => (
              <button
                type="button"
                role="tab"
                aria-selected={camera === item.id}
                className={camera === item.id ? 'active' : ''}
                disabled={!item.available}
                onClick={() => setCamera(item.id)}
                key={item.id}
              >
                <Camera size={14} /> {item.label}
                {!item.available && <span>missing</span>}
              </button>
            ))}
          </div>
          <DriveMedia
            driveId={driveId}
            camera={camera}
            durationUs={detail.duration_us}
            playheadUs={playheadUs}
            onPlayhead={seek}
            onSyncStatus={setPlaybackSync}
            telemetrySha256={detail.telemetry_generation?.ndjson_sha256}
            timelineVersion={detail.telemetry_generation?.timeline_version}
            refreshWhileProcessing={detail.readiness !== 'ready'}
          />
          {currentSegment && selectedCameraReadiness !== 'ready' && (
            <div className="media-gap-notice">
              <AlertTriangle size={16} />
              Segment {currentSegment.index} has no ready {selectedCameraLabel} AV1 derivative
              {selectedCameraReadiness ? ` (${selectedCameraReadiness})` : ''}.
            </div>
          )}
        </div>
        <Panel
          className="studio-inspector"
          kicker={
            playbackSync.mode === 'exact'
              ? 'Exact video ↔ rlog frame'
              : playbackSync.mode === 'approximate'
                ? 'Approximate video alignment'
                : 'Video and telemetry'
          }
          title="Frame inspector"
        >
          {focusedSeriesLoading && !focusedSeries
            ? <LoadingState label="Indexing the playhead" />
            : focusedSeriesError && !focusedSeries
              ? <ErrorState error={focusedSeriesError} title="Focused telemetry unavailable" />
              : <Inspector series={focusedSeries} playheadUs={playheadUs} />}
        </Panel>
      </div>

      <Panel className="timeline-panel" kicker="Whole drive" title="Events & availability">
        {seriesError && !series && (
          <div className="notice notice-warning telemetry-marker-warning">
            <AlertTriangle size={16} />
            Marker timeline unavailable: {seriesError.message}
          </div>
        )}
        {seriesLoading && !series ? (
          <LoadingState label="Loading whole-drive telemetry markers" />
        ) : series ? (
          <>
            {series.markers_truncated && (
              <div className="notice notice-warning telemetry-marker-warning">
                <AlertTriangle size={16} />
                The drive contains more than 10,000 telemetry markers. This timeline is incomplete.
              </div>
            )}
            <EventTimeline
              events={series.events}
              gaps={series.gaps}
              startUs={0}
              endUs={detail.duration_us}
              playheadUs={playheadUs}
              onSeek={seek}
            />
          </>
        ) : (
          <div className="segment-timeline">
            {detail.segments.map((segment) => (
              <button
                type="button"
                key={segment.index}
                className={`segment-block segment-${segment.state}`}
                style={{ flex: Math.max(1, segment.end_us - segment.start_us) }}
                onClick={() => seek(segment.start_us)}
                title={[
                  `Segment ${segment.index}: ${segment.state}`,
                  ...(segment.missing ?? []),
                ].join(' · ')}
              />
            ))}
          </div>
        )}
      </Panel>

      {tuneMode ? (
        <Panel className="tune-panel">
          <TuneWorkbench
            driveId={driveId}
            playheadUs={playheadUs}
            durationUs={detail.duration_us}
            models={modelsState.data}
            modelsError={modelsState.error}
            simulationEligible={detail.simulation_eligible}
            simulationEligibilityReasons={detail.simulation_eligibility_reasons}
            telemetrySha256={detail.telemetry_generation?.ndjson_sha256}
            timelineVersion={detail.telemetry_generation?.timeline_version}
          />
        </Panel>
      ) : (
        <div className="telemetry-layout">
          {focusedSeriesError && !focusedSeries ? (
            <Panel><ErrorState error={focusedSeriesError} title="Focused rlog series unavailable" /></Panel>
          ) : focusedSeriesLoading && !focusedSeries ? (
            <Panel><LoadingState label="Loading focused synchronized telemetry" /></Panel>
          ) : !focusedSeries ? (
            <Panel><EmptyState title="No telemetry index" description="Video playback remains available while rlog extraction is pending." icon={<FileCode2 />} /></Panel>
          ) : (
            <>
              {focusedSeries.markers_truncated && (
                <div className="notice notice-warning telemetry-marker-warning">
                  <AlertTriangle size={16} />
                  This focused window contains more than 2,000 telemetry markers; event overlays are incomplete.
                </div>
              )}
              <Panel
                className="telemetry-chart-panel"
                kicker={`Focused ${formatDurationUs(focusedEnd - focusedStart, true)} window`}
                title="Lateral tracking"
                action={<span className="chart-window-label">{formatDurationUs(focusedStart)} – {formatDurationUs(focusedEnd)}</span>}
              >
                <SignalChart
                  series={[
                    { ...desired, color: '#86efc2' },
                    { ...actual, color: '#63b3ff' },
                  ]}
                  startUs={focusedStart}
                  endUs={focusedEnd}
                  playheadUs={playheadUs}
                  onSeek={seek}
                  height={205}
                />
              </Panel>
              <Panel className="telemetry-chart-panel" kicker="Controller" title="Applied torque">
                <SignalChart series={[{ ...torque, color: '#f4bb62' }]} startUs={focusedStart} endUs={focusedEnd} playheadUs={playheadUs} onSeek={seek} height={170} />
              </Panel>
              <Panel className="telemetry-chart-panel" kicker="Vehicle" title="Steering angle">
                <SignalChart
                  series={[{ ...angle, color: '#c39cff' }]}
                  startUs={focusedStart}
                  endUs={focusedEnd}
                  playheadUs={playheadUs}
                  onSeek={seek}
                  height={170}
                />
              </Panel>
              <Panel className="telemetry-chart-panel" kicker="Vehicle" title="Speed">
                <SignalChart
                  series={[{ ...speed, color: '#9ba8b0' }]}
                  startUs={focusedStart}
                  endUs={focusedEnd}
                  playheadUs={playheadUs}
                  onSeek={seek}
                  height={170}
                />
              </Panel>
            </>
          )}
        </div>
      )}

      <Panel className="drive-integrity" kicker="Backup integrity" title="Drive provenance">
        <div className="integrity-grid">
          <div><CheckCircle2 /><span>Segments</span><strong>{detail.ready_segments} / {detail.segment_count} durable</strong><ProgressBar value={percent(detail.ready_segments, detail.segment_count)} /></div>
          <div><Film /><span>Playback</span><strong>{detail.cameras.filter((item) => item.available).map((item) => item.label).join(', ') || 'none'}</strong><small>Server-side AV1 derivatives</small></div>
          <div><FileCode2 /><span>Rlogs</span><strong>{rlogBackupLabel(detail)}</strong><small>{telemetryStatusLabel(detail.telemetry_status)} · {detail.rlog_hashes?.length ?? 0} source hashes</small></div>
          <div>
            <Clock3 />
            <span>Sync</span>
            <strong>
              {playbackSync.mode === 'exact'
                ? 'Verified frame timeline'
                : playbackSync.mode === 'approximate'
                  ? 'Approximate alignment'
                  : 'Unavailable at playhead'}
            </strong>
            <small>
              {playbackSync.mode === 'exact'
                ? 'Frame PTS → route-relative rlog time'
                : playbackSync.reason ?? 'Waiting for media timeline'}
            </small>
          </div>
        </div>
        {detail.telemetry_generation && (
          <details className="provenance telemetry-provenance">
            <summary>
              Telemetry generation provenance
              <ChevronDown size={14} />
            </summary>
            <div>
              <p>
                <span>Publication</span>
                <strong>
                  {detail.telemetry_generation.publication_ready
                    ? 'Stable generation'
                    : `${detail.telemetry_generation.state} · not stable for bookmarks`}
                </strong>
              </p>
              <p>
                <span>Timeline</span>
                <strong>{detail.telemetry_generation.timeline_version ?? 'not published'}</strong>
              </p>
              <p>
                <span>Telemetry SHA-256</span>
                <strong className="mono">{detail.telemetry_generation.ndjson_sha256}</strong>
              </p>
              <p>
                <span>Source fingerprint</span>
                <strong className="mono">{detail.telemetry_generation.source_fingerprint}</strong>
              </p>
              <p>
                <span>Extractor</span>
                <strong>
                  {[detail.telemetry_generation.extractor, detail.telemetry_generation.extractor_version]
                    .filter(Boolean)
                    .join(' · ') || 'not published'}
                </strong>
              </p>
              <p>
                <span>StarPilot commit</span>
                <strong className="mono">{detail.telemetry_generation.source_starpilot_commit ?? 'not published'}</strong>
              </p>
              <p>
                <span>Source rlogs</span>
                <strong className="telemetry-hash-list">
                  {detail.telemetry_generation.source_rlogs.length
                    ? detail.telemetry_generation.source_rlogs.map((source, index) => (
                        <span className="mono" key={`${source.segment_num ?? index}-${source.sha256 ?? index}`}>
                          {source.segment_num != null && `segment ${source.segment_num} · `}
                          {source.sha256 ?? 'hash unavailable'}
                        </span>
                      ))
                    : 'No source rlog hashes published'}
                </strong>
              </p>
            </div>
          </details>
        )}
      </Panel>
    </div>
  )
}
