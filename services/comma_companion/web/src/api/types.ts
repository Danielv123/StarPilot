export type HealthState = 'healthy' | 'warning' | 'error' | 'offline' | 'idle' | 'running'

export interface StorageSummary {
  used_bytes: number
  capacity_bytes?: number
  free_bytes?: number
  cataloged_bytes: number
  raw_bytes: number
  derived_bytes: number
}

export interface Overview {
  generated_at: string
  devices_online: number
  devices_total: number
  upload_bps: number
  pending_upload_bytes: number
  drives_total: number
  drives_ready: number
  drives_by_readiness: DriveReadinessCounts
  jobs_active: number
  worker_transcode_jobs_remaining: number
  worker_eta_seconds?: number
  storage: StorageSummary
  services: Array<{
    id: string
    label: string
    state: HealthState
    detail: string
    updated_at: string
  }>
  active_uploads: Upload[]
  recent_drives: Drive[]
}

export interface InboundStatus {
  generated_at: string
  devices_online: number
  devices_total: number
  devices_onroad: number
  devices_parked: number
  devices_road_state_unknown: number
  upload_bps: number
  pending_upload_bytes: number
  unuploaded_bytes: number
  unuploaded_files: number
  protected_spool_bytes: number
  backlog_scope: 'full' | 'protected' | 'server'
  backlog_scan_complete: boolean
  device_metrics_at?: string
  device_metrics_stale: boolean
  server_pending_bytes: number
  bytes_received: number
}

export interface Device {
  id: string
  name: string
  dongle_id?: string
  state: HealthState
  online: boolean
  onroad?: boolean
  offroad?: boolean
  last_seen_at?: string
  started_at?: string
  software_version?: string
  git_branch?: string
  git_commit?: string
  vehicle?: string
  ip_address?: string
  network_type?: string
  network_metered?: boolean
  upload_bps?: number
  queue_bytes?: number
  unuploaded_bytes?: number
  unuploaded_files?: number
  backlog_scanned_at?: string
  backlog_scan_complete?: boolean
  spool_bytes?: number
  spool_capacity_bytes?: number
  free_space_bytes?: number
  temperature_c?: number
  battery_percent?: number
  capabilities?: string[]
  current_drive_id?: string
}

export type UploadState = 'queued' | 'uploading' | 'verifying' | 'processing' | 'ready' | 'failed' | 'paused' | 'canceled'

export interface Upload {
  id: string
  file_id?: string
  source_kind?: 'device' | 'importer'
  device_id: string
  device_name?: string
  route_id?: string
  segment?: number
  filename: string
  kind: string
  size_bytes: number
  received_bytes: number
  state: UploadState
  durable?: boolean
  upload_bps?: number
  eta_seconds?: number
  created_at: string
  updated_at: string
  error?: string
  jobs?: Array<{
    id?: string
    label: string
    state: HealthState
    progress?: number
    attempts?: number
    max_attempts?: number
    error?: string
    updated_at?: string
  }>
}

export interface UploadSnapshot {
  active_uploads: number
  failed_uploads: number
  completed_uploads: number
  bytes_received: number
  bytes_expected: number
  pending_bytes: number
  bytes_per_second_60s: number
  by_device: Record<string, {
    active_uploads: number
    bytes_received: number
    pending_bytes: number
  }>
}

export interface Job {
  id: string
  type: string
  label: string
  upload_id?: string
  drive_id?: string
  artifact_id?: string
  retry_of_job_id?: string
  state: 'queued' | 'leased' | 'running' | 'succeeded' | 'failed' | 'canceled'
  progress: number
  attempts: number
  max_attempts: number
  retryable?: boolean
  error?: string
  result: Record<string, unknown>
  available_at?: string
  cancel_requested_at?: string
  created_at: string
  updated_at: string
  completed_at?: string
}

export type DriveReadiness = 'ready' | 'processing' | 'partial' | 'uploading' | 'importing' | 'failed'
export type CatalogDriveReadiness = Exclude<DriveReadiness, 'uploading'>
export type DriveReadinessCounts = Record<CatalogDriveReadiness, number>
export type TelemetryStatus = 'ready' | 'awaiting_rlogs' | 'awaiting_inventory' | 'extracting' | 'refreshing' | 'finalizing'

export interface Drive {
  id: string
  device_id: string
  route_name: string
  started_at: string
  ended_at?: string
  duration_us: number
  distance_m?: number
  readiness: DriveReadiness
  segment_count: number
  ready_segments: number
  expected_media: number
  ready_media: number
  missing_media: number
  failed_media: number
  pruned_media: number
  raw_video_pruning_required: boolean
  backup_bytes_received: number
  backup_bytes_expected: number
  artifact_count: number
  expected_rlogs: number
  archived_rlogs: number
  rlog_backup_complete: boolean
  cameras: Camera[]
  telemetry_ready: boolean
  telemetry_status: TelemetryStatus
  vehicle?: string
  software_version?: string
  model_name?: string
  location_start?: string
  location_end?: string
  thumbnail_url?: string
  poster_url?: string
  raw_bytes?: number
  derived_bytes?: number
  stored_bytes?: number
  flags?: string[]
}

export interface DriveCatalogPage {
  items: Drive[]
  total: number
  limit: number
  offset: number
  summary: {
    duration_us: number
    stored_bytes: number
    by_readiness: DriveReadinessCounts
  }
}

export interface Camera {
  id: string
  label: string
  available: boolean
  codec?: string
  width?: number
  height?: number
  fps?: number
}

export interface DriveDetail extends Drive {
  segments: Array<{
    index: number
    start_us: number
    end_us: number
    state: DriveReadiness
    missing?: string[]
    camera_readiness?: Record<string, DriveReadiness>
    expected_streams: Array<{
      role: string
      artifact_type: string
      camera?: string
      manifest_status: 'present' | 'missing'
      relative_path?: string
      archive_status: 'missing' | 'stored' | 'verified' | 'ready'
      media_status: 'not_required' | 'pending' | 'ready' | 'failed'
    }>
  }>
  route_inventory?: {
    manifest_sha256: string
    generation: number
    state: 'complete' | 'partial'
    route_closed: boolean
    capability_source: 'configured+route_union' | 'route_union_unconfigured'
    closure_evidence: string[]
    expected_streams: Array<{
      role: string
      root_name: string
      artifact_type: string
      camera?: string
    }>
    missing_segment_numbers: number[]
    declared_file_count: number
    archived_file_count: number
    missing_file_count: number
  }
  telemetry_start_us?: number
  telemetry_end_us?: number
  rlog_hashes?: string[]
  telemetry_generation?: {
    state: string
    schema_version: number
    ndjson_sha256: string
    source_fingerprint: string
    timeline_version?: string
    publication_ready: boolean
    start_t_us?: number
    end_t_us?: number
    extractor?: string
    extractor_version?: string
    source_starpilot_commit?: string
    source_rlogs: Array<{
      segment_num?: number
      log_type?: string
      sha256?: string
      size_bytes?: number
      compression?: string
    }>
    vehicle: Record<string, unknown>
    route_software: Record<string, unknown>
    completeness: Record<string, unknown>
  }
  simulation_eligible: boolean
  simulation_eligibility_reasons: Array<{
    code: string
    message: string
    details?: Record<string, unknown>
  }>
  notes?: string
}

export interface MediaManifestItem {
  segment_number: number
  start_t_us: number | null
  duration_us: number | null
  artifact_id: string
  url: string
  mime_type?: string
  codec?: string
  fps?: number
  sync_mode: 'exact' | 'approximate'
  sync_url: string | null
  frame_index_artifact_id: string | null
  frame_index_url: string | null
  sync_reason: string | null
  video_sha256: string | null
  timeline_origin: 'stable' | 'provisional' | null
}

export interface MediaManifest {
  drive_id: string
  camera: string
  synchronized: boolean
  items: MediaManifestItem[]
}

export interface MediaSyncPoint {
  segment_frame_id: number
  pts_us: number
  duration_us: number
  drive_t_us: number
  keyframe: boolean
}

export interface MediaSyncIndex {
  drive_id: string
  camera: string
  segment_number: number
  video_artifact_id: string
  video_sha256: string
  frame_index_artifact_id: string
  frame_index_sha256: string
  telemetry_sha256: string
  timeline_version: string
  timeline_origin: 'stable' | 'provisional'
  mode: 'exact'
  coverage: {
    start_t_us: number
    end_t_us: number
    first_pts_us: number
    last_end_pts_us: number
  }
  points: MediaSyncPoint[]
}

export interface SeriesPoint {
  t_us: number
  value: number | null
}

export interface SignalSeries {
  id: string
  label: string
  unit: string
  kind?: 'continuous' | 'step' | 'event'
  color?: string
  points: SeriesPoint[]
}

export interface TimelineEvent {
  id: string
  lane: 'active' | 'driver_overlay' | 'alert' | 'gap' | 'saturation'
  start_us: number
  end_us: number
  label: string
  severity?: 'info' | 'warning' | 'critical'
}

export interface DriveSeries {
  drive_id: string
  ndjson_sha256: string
  timeline_version: string
  timeline_origin: 'stable' | 'provisional'
  start_us: number
  end_us: number
  signals: SignalSeries[]
  events: TimelineEvent[]
  gaps: Array<{
    start_us: number
    end_us: number
    reason: string
    kind?: string
    severity?: 'info' | 'warning' | 'critical'
  }>
  markers_truncated: boolean
}

export interface ModelSummary {
  id: string
  name: string
  version: string
  state: HealthState
  vehicle: string
  horizon_us: number
  history_us: number
  hash: string
  mode: string
  enabled: boolean
  eligible: boolean
  eligibility_reasons: string[]
  last_used_at?: string
  description?: string
}

export interface ParameterSchema {
  model_id: string
  title: string
  description?: string
  parameters: Array<{
    id: string
    label: string
    description?: string
    group: string
    type: 'number' | 'integer' | 'boolean' | 'enum'
    unit?: string
    default: number | boolean | string
    minimum?: number
    maximum?: number
    step?: number
    options?: Array<{ value: string; label: string }>
    scope?: string
    runtime_supported?: boolean
  }>
}

export interface SimulationRequest {
  drive_id: string
  model_id: string
  start_us: number
  horizon_us: number
  telemetry_sha256: string
  timeline_version: string
  parameters: Record<string, number | boolean | string>
}

export interface SimulationTrace {
  id: 'recorded' | 'baseline' | 'candidate' | 'desired'
  label: string
  unit: string
  points: SeriesPoint[]
}

export interface SimulationWarning {
  code: string
  severity: 'info' | 'warning' | 'blocker'
  message: string
  details?: Record<string, unknown>
}

export interface SimulationResult {
  id: string
  state: 'complete' | 'running' | 'failed'
  start_us: number
  end_us: number
  exact_baseline: boolean
  traces: SimulationTrace[]
  metrics: Array<{
    label: string
    baseline: number
    candidate: number
    unit: string
    objective: 'lower' | 'absolute_lower' | 'higher'
  }>
  warnings: SimulationWarning[]
  uncertainty: {
    baseline_std: SeriesPoint[]
    candidate_std: SeriesPoint[]
    baseline_disagreement_mean_normalized?: number
    baseline_disagreement_p95_normalized?: number
    candidate_disagreement_mean_normalized?: number
    candidate_disagreement_p95_normalized?: number
    maximum_disagreement_p95_normalized?: number
  }
  validity: {
    valid: boolean
    flags: string[]
  }
  provenance: {
    model_name: string
    model_hash: string
    controller_mode: string
    starpilot_commit: string
    extractor_version: string
    rlog_hashes: string[]
    telemetry_hash?: string
    timeline_version?: string
    input_alignment?: string
  }
}

export interface SimulationProgress {
  simulation_id: string
  job_id: string
  state: string
  progress: number
  cancel_requested: boolean
}

export interface ActivityItem {
  id: string
  created_at: string
  actor: string
  action: string
  target: string
  state: HealthState
  detail?: string
}

export interface Settings {
  archive_path: string
  raw_log_retention_enabled: true
  raw_video_retention_enabled: boolean
  transcode_codec: string
  transcode_crf: number
  worker_concurrency: number
  metered_uploads_allowed: boolean
  timezone: string
}

export interface Session {
  authenticated: boolean
  username?: string
  expires_at?: string
}

export interface Command {
  type:
    | 'rescan'
    | 'pause'
    | 'resume'
    | 'retry_upload'
    | 'cancel_upload'
    | 'restart_agent'
    | 'restart_starpilot'
    | 'reboot_device'
    | 'shutdown_device'
  args?: Record<string, string | number | boolean>
  expires_in_seconds?: number
}

export interface CommandReceipt {
  id: string
  device_id?: string
  type?: string
  state: 'queued' | 'delivered' | 'running' | 'succeeded' | 'failed' | 'rejected' | 'expired' | 'canceled' | 'accepted'
  message?: string
  error?: string
  issued_at?: string
  expires_at?: string
  finished_at?: string
}
