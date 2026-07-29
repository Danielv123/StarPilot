import type {
  ActivityItem,
  Command,
  CommandReceipt,
  Device,
  Drive,
  DriveCatalogPage,
  DriveDetail,
  DriveReadiness,
  DriveSeries,
  Job,
  MediaManifest,
  MediaSyncIndex,
  ModelSummary,
  Overview,
  ParameterSchema,
  Session,
  Settings,
  SimulationRequest,
  SimulationProgress,
  SimulationResult,
  TimelineEvent,
  Upload,
  UploadSnapshot,
} from './types'
import { getCsrfToken } from '../utils'

type DemoApi = typeof import('./demo')['demoApi']

export const demoMode =
  import.meta.env.DEV &&
  import.meta.env.VITE_DEMO_MODE === 'true'
export const sessionExpiredEvent = 'comma-companion:session-expired'
const base = '/api/v1'
let demoApiPromise: Promise<DemoApi> | undefined

async function loadDemoApi(): Promise<DemoApi> {
  if (!demoMode || !import.meta.env.DEV) {
    throw new Error('Demo data is only available from the development server.')
  }
  demoApiPromise ??= import('./demo').then((module) => module.demoApi)
  return demoApiPromise
}

export class ApiError extends Error {
  status: number
  code?: string
  detail?: unknown

  constructor(message: string, status: number, code?: string, detail?: unknown) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.detail = detail
  }
}

export function isSessionExpiryError(error: unknown): error is ApiError {
  return error instanceof ApiError &&
    error.status === 401 &&
    ['authentication_required', 'session_expired', 'invalid_session'].includes(error.code ?? '')
}

type ApiEnvelope<T> = T | { data: T }

function unwrap<T>(value: ApiEnvelope<T>): T {
  if (value && typeof value === 'object' && 'data' in value) return (value as { data: T }).data
  return value as T
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !(init.body instanceof FormData) && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  if (init.method && init.method !== 'GET' && init.method !== 'HEAD') {
    headers.set('Idempotency-Key', headers.get('Idempotency-Key') ?? crypto.randomUUID())
    const csrfToken = getCsrfToken()
    if (csrfToken) headers.set('X-CSRF-Token', csrfToken)
  }
  headers.set('Accept', 'application/json')

  let response: Response
  try {
    response = await fetch(`${base}${path}`, {
      credentials: 'include',
      ...init,
      headers,
    })
  } catch (error) {
    throw new ApiError(error instanceof Error ? error.message : 'The archive server is unreachable.', 0, 'network_error')
  }

  if (response.status === 204) return undefined as T
  const contentType = response.headers.get('content-type') ?? ''
  const body = contentType.includes('application/json')
    ? await response.json()
    : await response.text()

  if (!response.ok) {
    const record = body && typeof body === 'object' ? (body as Record<string, unknown>) : undefined
    const nested = record?.error && typeof record.error === 'object'
      ? record.error as Record<string, unknown>
      : undefined
    const error = new ApiError(
      String(nested?.message ?? record?.message ?? record?.detail ?? response.statusText ?? 'Request failed'),
      response.status,
      typeof nested?.code === 'string' ? nested.code : typeof record?.code === 'string' ? record.code : undefined,
      nested?.details ?? body,
    )
    if (isSessionExpiryError(error)) window.dispatchEvent(new Event(sessionExpiredEvent))
    throw error
  }
  return unwrap(body as ApiEnvelope<T>)
}

function query(values: Record<string, string | number | boolean | undefined>): string {
  const params = new URLSearchParams()
  Object.entries(values).forEach(([key, value]) => {
    if (value != null && value !== '') params.set(key, String(value))
  })
  const result = params.toString()
  return result ? `?${result}` : ''
}

function apiRelativePath(url: string): string {
  const parsed = new URL(url, window.location.origin)
  if (parsed.origin !== window.location.origin) {
    throw new ApiError('The media timeline URL is not on this archive server.', 400, 'invalid_sync_url')
  }
  if (parsed.pathname !== base && !parsed.pathname.startsWith(`${base}/`)) {
    throw new ApiError('The media timeline URL is outside the archive API.', 400, 'invalid_sync_url')
  }
  return `${parsed.pathname.slice(base.length)}${parsed.search}`
}

interface RawDashboard {
  generated_at: string
  devices_total: number
  devices_online: number
  drives_total: number
  drives_ready: number
  drives_by_readiness: Record<string, number>
  segments_total: number
  artifacts_total: number
  raw_bytes: number
  derived_bytes: number
  storage_cataloged_bytes: number
  storage_capacity_bytes: number | null
  storage_used_bytes: number | null
  storage_free_bytes: number | null
  upload: {
    active_uploads: number
    failed_uploads: number
    completed_uploads: number
    bytes_received: number
    bytes_expected: number
    pending_bytes: number
    bytes_per_second_60s: number
  }
  commands_by_state: Record<string, number>
  jobs_by_state: Record<string, number>
  worker: {
    last_seen: string | null
    stale: boolean
    online: boolean
  }
  archive: {
    available: boolean
    writable: boolean
  }
}

interface RawDevice {
  id: string
  display_name: string
  enrolled_at: string
  last_seen_at: string | null
  online: boolean
  offroad: boolean | null
  agent_version: string | null
  software_version: string | null
  network_type: string | null
  state: string | null
  capabilities: string[]
  metrics: Record<string, unknown>
}

interface RawUpload {
  id: string
  upload_id: string
  file_id?: string | null
  source_kind?: 'device' | 'importer' | null
  device_id: string
  relative_path: string
  route_name: string | null
  segment_number: number | null
  artifact_type: string
  camera: string | null
  offset: number
  length: number
  status: 'receiving' | 'finalizing' | 'complete' | 'failed' | 'canceled'
  durable: boolean
  error: string | null
  created_at: string
  updated_at: string
  bytes_per_second: number
}

interface RawDrive {
  id: string
  device_id: string
  route_name: string
  started_at: string | null
  ended_at: string | null
  duration_us: number | null
  segment_count: number
  ready_segments: number
  expected_media: number
  ready_media: number
  missing_media: number
  failed_media: number
  artifact_count: number
  telemetry_ready: boolean
  readiness: 'importing' | 'processing' | 'ready' | 'partial' | 'failed'
  cameras: Array<{
    id: string
    label: string
    available: boolean
    codec: string | null
    width: number | null
    height: number | null
    fps: number | null
  }>
  vehicle: string | null
  distance_m: number | null
  location_start: string | null
  location_end: string | null
  raw_bytes: number
  derived_bytes: number
  stored_bytes: number
  poster_url: string | null
  thumbnail_url: string | null
  created_at: string
}

interface RawDriveCatalogPage {
  items: RawDrive[]
  total: number
  limit: number
  offset: number
  summary: {
    duration_us: number
    stored_bytes: number
    by_readiness: Record<string, number>
  }
}

interface RawArtifact {
  id: string
  kind: string
  camera: string | null
  size: number
  mime_type: string | null
  codec: string | null
  duration_us: number | null
  status: string
}

interface RawSegment {
  id: string
  number: number
  start_t_us: number | null
  duration_us: number | null
  artifacts: RawArtifact[]
  expected_streams: Array<{
    role: string
    artifact_type: string
    camera: string | null
    manifest_status: 'present' | 'missing'
    relative_path: string | null
    archive_status: 'missing' | 'stored' | 'verified' | 'ready'
    media_status: 'not_required' | 'pending' | 'ready' | 'failed'
  }>
}

interface RawDriveDetail extends RawDrive {
  segments: RawSegment[]
  route_inventory: {
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
      camera: string | null
    }>
    missing_segment_numbers: number[]
    declared_file_count: number
    archived_file_count: number
    missing_file_count: number
  } | null
  telemetry_generation: {
    state: string
    schema_version: number
    ndjson_sha256: string
    source_fingerprint: string
    timeline_version: string | null
    publication_ready: boolean
    start_t_us: number | null
    end_t_us: number | null
    extractor: string | null
    extractor_version: string | null
    source_starpilot_commit: string | null
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
  } | null
  simulation_eligible: boolean
  simulation_eligibility_reasons: Array<{
    code: string
    message: string
    details?: Record<string, unknown>
  }>
}

interface RawSeries {
  drive_id: string
  ndjson_sha256: string
  timeline_version: string
  timeline_origin: 'stable' | 'provisional'
  start_t_us: number
  end_t_us: number
  signals: Array<{
    signal: string
    unit: string | null
    kind: 'continuous' | 'step' | 'event'
    points: Array<{ t_us: number; value: number | boolean | string | null }>
  }>
  markers: Array<{
    id: string
    kind: string
    start_t_us: number
    end_t_us: number
    severity: string | null
    label: string | null
    attributes: Record<string, unknown>
  }>
  markers_truncated: boolean
}

interface RawModel {
  sha256: string
  name: string
  mode: string
  enabled: boolean
  eligible: boolean
  metadata?: Record<string, unknown>
}

interface RawSimulatorCapabilities {
  available: boolean
  modes: string[]
  models: RawModel[]
  history_required_us: number
  default_horizon_us: number
  maximum_horizon_us: number
  parameter_schema: unknown
}

interface RawSimulationAccepted {
  id: string
  job_id: string
  state: 'queued'
}

interface RawSimulationView {
  id: string
  drive_id: string
  job_id: string
  t_us: number
  horizon_us: number
  model_hash: string
  mode: string
  parameters: Record<string, unknown>
  telemetry_sha256: string
  timeline_version: string | null
  state: string
  progress: number
  result: Record<string, unknown>
  error: string | null
}

interface RawJob {
  id: string
  type: string
  upload_id: string | null
  drive_id: string | null
  artifact_id: string | null
  retry_of_job_id: string | null
  state: 'queued' | 'leased' | 'running' | 'succeeded' | 'failed' | 'canceled'
  progress: number
  attempts: number
  max_attempts: number
  retryable: boolean | null
  error: string | null
  result: Record<string, unknown>
  available_at: string | null
  cancel_requested_at: string | null
  created_at: string
  updated_at: string
  completed_at: string | null
}

interface RawAuditEvent {
  id: number
  actor_type: string
  actor_id: string | null
  action: string
  resource_type: string | null
  resource_id: string | null
  details: Record<string, unknown>
  created_at: string
}

function metricNumber(metrics: Record<string, unknown>, ...keys: string[]): number | undefined {
  for (const key of keys) {
    const value = metrics[key]
    if (typeof value === 'number' && Number.isFinite(value)) return value
  }
  return undefined
}

function metricString(metrics: Record<string, unknown>, ...keys: string[]): string | undefined {
  for (const key of keys) {
    const value = metrics[key]
    if (typeof value === 'string' && value) return value
  }
  return undefined
}

function metricRecord(metrics: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = metrics[key]
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function metricArray(metrics: Record<string, unknown>, key: string): Record<string, unknown>[] {
  const value = metrics[key]
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> =>
        Boolean(item) && typeof item === 'object' && !Array.isArray(item))
    : []
}

export function normalizeDevice(raw: RawDevice): Device {
  const metrics = raw.metrics ?? {}
  const host = metricRecord(metrics, 'host')
  const starpilot = metricRecord(host, 'starpilot')
  const temperatures = metricArray(host, 'temperatures')
    .flatMap((entry) => typeof entry.celsius === 'number' ? [entry.celsius] : [])
  const batteryPercent = metricArray(host, 'power_supplies')
    .flatMap((entry) => {
      const supplyMetrics = metricRecord(entry, 'metrics')
      return typeof supplyMetrics.capacity === 'number' ? [supplyMetrics.capacity] : []
    })[0]
  const offroadKnown = raw.offroad != null
  return {
    id: raw.id,
    name: raw.display_name,
    state: !raw.online
      ? 'offline'
      : raw.offroad === false
        ? 'running'
        : !offroadKnown || raw.state === 'warning' || raw.state === 'storage_pressure'
          ? 'warning'
          : raw.state === 'uploading' ? 'running' : 'healthy',
    online: raw.online,
    onroad: offroadKnown ? raw.offroad === false : undefined,
    offroad: raw.offroad ?? undefined,
    last_seen_at: raw.last_seen_at ?? undefined,
    software_version: raw.software_version ?? undefined,
    git_branch: metricString(starpilot, 'branch') ?? metricString(metrics, 'git_branch', 'branch'),
    git_commit: metricString(starpilot, 'commit') ?? metricString(metrics, 'git_commit', 'commit'),
    vehicle: metricString(host, 'vehicle', 'car_fingerprint') ?? metricString(metrics, 'vehicle', 'car_fingerprint'),
    ip_address: metricString(metrics, 'ip_address'),
    network_type: raw.network_type ?? undefined,
    network_metered: metrics.network_metered === true,
    upload_bps: metricNumber(metrics, 'upload_bps', 'bytes_per_second'),
    queue_bytes: metricNumber(metrics, 'queue_bytes', 'pending_bytes'),
    spool_bytes: metricNumber(metrics, 'spool_bytes'),
    spool_capacity_bytes: metricNumber(metrics, 'spool_capacity_bytes'),
    free_space_bytes:
      metricNumber(host, 'data_free_bytes') ??
      metricNumber(metrics, 'free_space_bytes', 'storage_free_bytes', 'disk_free_bytes'),
    temperature_c:
      temperatures.length ? Math.max(...temperatures) : metricNumber(metrics, 'temperature_c', 'device_temperature_c'),
    battery_percent: batteryPercent ?? metricNumber(metrics, 'battery_percent'),
    capabilities: raw.capabilities,
    current_drive_id: metricString(host, 'current_route') ?? metricString(metrics, 'current_route'),
  }
}

function normalizeUpload(raw: RawUpload): Upload {
  const filename = raw.relative_path.split(/[\\/]/).at(-1) || raw.relative_path
  const state: Upload['state'] =
    raw.status === 'failed'
      ? 'failed'
      : raw.status === 'canceled'
        ? 'canceled'
      : raw.status === 'finalizing'
        ? 'verifying'
      : raw.status === 'complete'
        ? 'ready'
        : 'uploading'
  return {
    id: raw.id,
    file_id: raw.file_id ?? undefined,
    source_kind: raw.source_kind ?? undefined,
    device_id: raw.device_id,
    route_id: raw.route_name ?? undefined,
    segment: raw.segment_number ?? undefined,
    filename,
    kind: raw.artifact_type,
    size_bytes: raw.length,
    received_bytes: raw.offset,
    state,
    durable: raw.durable,
    upload_bps: raw.bytes_per_second,
    eta_seconds: raw.bytes_per_second > 0 ? Math.ceil((raw.length - raw.offset) / raw.bytes_per_second) : undefined,
    created_at: raw.created_at,
    updated_at: raw.updated_at,
    error: raw.error ?? undefined,
  }
}

function cameraLabel(id: string): string {
  return id === 'road' ? 'Road' : id === 'wide' ? 'Wide' : id === 'driver' ? 'Driver' : id
}

function normalizeDrive(raw: RawDrive): Drive {
  return {
    id: raw.id,
    device_id: raw.device_id,
    route_name: raw.route_name,
    started_at: raw.started_at ?? raw.created_at,
    ended_at: raw.ended_at ?? undefined,
    duration_us: raw.duration_us ?? 0,
    readiness: raw.readiness,
    segment_count: raw.segment_count,
    ready_segments: raw.ready_segments,
    cameras: raw.cameras.map((camera) => ({
      id: camera.id,
      label: camera.label,
      available: camera.available,
      codec: camera.codec ?? undefined,
      width: camera.width ?? undefined,
      height: camera.height ?? undefined,
      fps: camera.fps ?? undefined,
    })),
    telemetry_ready: raw.telemetry_ready,
    vehicle: raw.vehicle ?? undefined,
    distance_m: raw.distance_m ?? undefined,
    location_start: raw.location_start ?? undefined,
    location_end: raw.location_end ?? undefined,
    raw_bytes: raw.raw_bytes,
    derived_bytes: raw.derived_bytes,
    stored_bytes: raw.stored_bytes,
    poster_url: raw.poster_url ?? undefined,
    thumbnail_url: raw.thumbnail_url ?? undefined,
  }
}

export function normalizeDriveDetail(raw: RawDriveDetail): DriveDetail {
  const allArtifacts = raw.segments.flatMap((segment) => segment.artifacts)
  const cameraIds = [...new Set(
    [
      ...raw.cameras.map((camera) => camera.id),
      ...allArtifacts
        .filter((artifact) => artifact.camera)
        .map((artifact) => artifact.camera as string),
    ],
  )]
  const isDerivedVideo = (artifact: RawArtifact) =>
    artifact.codec?.toLowerCase().includes('av1') ||
    artifact.kind === 'derived_video' ||
    artifact.kind === 'video_av1'
  const actualEnds = raw.segments
    .filter((segment) => segment.start_t_us != null && segment.duration_us != null)
    .map((segment) => (segment.start_t_us as number) + (segment.duration_us as number))
  return {
    ...normalizeDrive(raw),
    duration_us: raw.duration_us ?? (actualEnds.length ? Math.max(...actualEnds) : 0),
    ready_segments: raw.ready_segments,
    cameras: cameraIds.map((id) => ({
      id,
      label: raw.cameras.find((camera) => camera.id === id)?.label ?? cameraLabel(id),
      available: raw.cameras.find((camera) => camera.id === id)?.available ?? allArtifacts.some(
        (artifact) =>
          artifact.camera === id &&
          artifact.status === 'ready' &&
          isDerivedVideo(artifact),
      ),
      codec: raw.cameras.find((camera) => camera.id === id)?.codec ?? undefined,
      width: raw.cameras.find((camera) => camera.id === id)?.width ?? undefined,
      height: raw.cameras.find((camera) => camera.id === id)?.height ?? undefined,
      fps: raw.cameras.find((camera) => camera.id === id)?.fps ?? undefined,
    })),
    telemetry_ready: raw.telemetry_ready,
    route_inventory: raw.route_inventory
      ? {
          ...raw.route_inventory,
          expected_streams: raw.route_inventory.expected_streams.map((stream) => ({
            ...stream,
            camera: stream.camera ?? undefined,
          })),
        }
      : undefined,
    telemetry_start_us: raw.telemetry_generation?.start_t_us ?? undefined,
    telemetry_end_us: raw.telemetry_generation?.end_t_us ?? undefined,
    rlog_hashes: raw.telemetry_generation?.source_rlogs.flatMap((source) =>
      source.sha256 ? [source.sha256] : []
    ),
    telemetry_generation: raw.telemetry_generation
      ? {
          ...raw.telemetry_generation,
          timeline_version: raw.telemetry_generation.timeline_version ?? undefined,
          start_t_us: raw.telemetry_generation.start_t_us ?? undefined,
          end_t_us: raw.telemetry_generation.end_t_us ?? undefined,
          extractor: raw.telemetry_generation.extractor ?? undefined,
          extractor_version: raw.telemetry_generation.extractor_version ?? undefined,
          source_starpilot_commit: raw.telemetry_generation.source_starpilot_commit ?? undefined,
        }
      : undefined,
    simulation_eligible: raw.simulation_eligible,
    simulation_eligibility_reasons: raw.simulation_eligibility_reasons,
    raw_bytes: raw.raw_bytes,
    derived_bytes: raw.derived_bytes,
    segments: raw.segments.map((segment) => {
      const synchronized = segment.start_t_us != null && segment.duration_us != null
      const cameraReadiness = Object.fromEntries(cameraIds.map((camera) => {
        const declared = segment.expected_streams.filter((stream) => stream.camera === camera)
        if (declared.length) {
          const state: DriveReadiness = declared.some((stream) => stream.media_status === 'failed')
            ? 'failed'
            : declared.every((stream) => stream.media_status === 'ready')
              ? 'ready'
              : declared.some((stream) => stream.manifest_status === 'missing')
                ? 'partial'
                : 'processing'
          return [camera, state]
        }
        const artifacts = segment.artifacts.filter((artifact) => artifact.camera === camera)
        const derived = artifacts.filter(isDerivedVideo)
        const state: DriveReadiness = derived.some((artifact) => artifact.status === 'ready')
          ? 'ready'
          : derived.some((artifact) => artifact.status === 'failed')
            ? 'failed'
            : artifacts.length || derived.length
              ? 'processing'
              : 'partial'
        return [camera, state]
      }))
      const cameraStates = Object.values(cameraReadiness)
      const state: DriveReadiness =
        synchronized && cameraStates.length > 0 && cameraStates.every((item) => item === 'ready')
          ? 'ready'
          : cameraStates.some((item) => item === 'failed')
            ? 'failed'
            : raw.readiness === 'ready'
              ? 'partial'
              : raw.readiness
      const missing = Object.entries(cameraReadiness)
        .filter(([, readiness]) => readiness !== 'ready')
        .map(([camera, readiness]) => `${cameraLabel(camera)} AV1 ${readiness}`)
      const missingRoles = segment.expected_streams
        .filter((stream) =>
          stream.manifest_status === 'missing' ||
          stream.archive_status === 'missing' ||
          stream.media_status === 'failed')
        .map((stream) => {
          const status = stream.media_status === 'failed'
            ? 'media failed'
            : stream.manifest_status === 'missing'
              ? 'missing from device manifest'
              : 'not archived'
          return `${stream.role}: ${status}`
        })
      return {
        index: segment.number,
        start_us: synchronized ? (segment.start_t_us as number) : -1,
        end_us: synchronized ? (segment.start_t_us as number) + (segment.duration_us as number) : -1,
        state,
        camera_readiness: cameraReadiness,
        missing: [...new Set([...missing, ...missingRoles])],
        expected_streams: segment.expected_streams.map((stream) => ({
          ...stream,
          camera: stream.camera ?? undefined,
          relative_path: stream.relative_path ?? undefined,
        })),
      }
    }),
  }
}

export function normalizeSeries(
  raw: RawSeries,
  expected?: {
    drive_id: string
    telemetry_sha256: string
    timeline_version: string
  },
): DriveSeries {
  if (
    expected &&
    (
      raw.drive_id !== expected.drive_id ||
      raw.ndjson_sha256 !== expected.telemetry_sha256 ||
      raw.timeline_version !== expected.timeline_version
    )
  ) {
    throw new ApiError(
      'The telemetry response belongs to a different published generation.',
      409,
      'telemetry_snapshot_mismatch',
      {
        expected,
        received: {
          drive_id: raw.drive_id,
          telemetry_sha256: raw.ndjson_sha256,
          timeline_version: raw.timeline_version,
        },
      },
    )
  }
  const labels: Record<string, string> = {
    'lateral.desired_acceleration': 'Desired lateral accel',
    'lateral.actual_acceleration': 'Actual lateral accel',
    'control.applied_torque': 'Applied torque',
    'vehicle.steering_angle': 'Steering angle',
    'vehicle.speed': 'Speed',
  }
  const markers = (raw.markers ?? []).filter((marker) =>
    typeof marker.id === 'string' &&
    typeof marker.kind === 'string' &&
    Number.isSafeInteger(marker.start_t_us) &&
    Number.isSafeInteger(marker.end_t_us) &&
    marker.end_t_us >= marker.start_t_us)
  const markerSeverity = (severity: string | null): 'info' | 'warning' | 'critical' =>
    severity === 'critical' || severity === 'warning' ? severity : 'info'
  const markerLabel = (marker: RawSeries['markers'][number]) =>
    marker.label?.trim() || marker.kind.replaceAll('_', ' ')
  const gapKinds = new Set(['telemetry_gap', 'camera_gap'])
  const gapInterval = (marker: RawSeries['markers'][number]) => {
    const gapUs = marker.attributes && typeof marker.attributes.gap_us === 'number'
      ? Math.max(0, Math.round(marker.attributes.gap_us))
      : 0
    return marker.end_t_us > marker.start_t_us || gapUs === 0
      ? { start_us: marker.start_t_us, end_us: marker.end_t_us }
      : {
          start_us: Math.max(raw.start_t_us, marker.start_t_us - gapUs),
          end_us: marker.start_t_us,
        }
  }
  const eventLane = (kind: string): TimelineEvent['lane'] => {
    if (kind === 'driver_overlay') return 'driver_overlay'
    if (kind === 'lateral_saturation') return 'saturation'
    if (kind === 'alert') return 'alert'
    if (
      [
        'controls_active',
        'engagement',
        'disengagement',
        'controls_state_observed',
        'onroad',
        'offroad',
        'onroad_interval',
      ].includes(kind)
    ) {
      return 'active'
    }
    return 'alert'
  }
  return {
    drive_id: raw.drive_id,
    ndjson_sha256: raw.ndjson_sha256,
    timeline_version: raw.timeline_version,
    timeline_origin: raw.timeline_origin,
    start_us: raw.start_t_us,
    end_us: raw.end_t_us,
    signals: raw.signals.map((signal) => ({
      id: signal.signal,
      label: labels[signal.signal] ?? signal.signal,
      unit: signal.unit ?? '',
      kind: signal.kind,
      points: signal.points.map((point) => ({
        t_us: point.t_us,
        value: typeof point.value === 'number' ? point.value : null,
      })),
    })),
    events: markers
      .filter((marker) => !gapKinds.has(marker.kind))
      .map((marker) => ({
        id: marker.id,
        lane: eventLane(marker.kind),
        start_us: marker.start_t_us,
        end_us: marker.end_t_us,
        label: markerLabel(marker),
        severity: markerSeverity(marker.severity),
      })),
    gaps: markers
      .filter((marker) => gapKinds.has(marker.kind))
      .map((marker) => ({
        ...gapInterval(marker),
        reason: markerLabel(marker),
        kind: marker.kind,
        severity: markerSeverity(marker.severity),
      })),
    markers_truncated: raw.markers_truncated === true,
  }
}

function normalizeJob(raw: RawJob): Job {
  return {
    id: raw.id,
    type: raw.type,
    label: raw.type.replaceAll('_', ' '),
    upload_id: raw.upload_id ?? undefined,
    drive_id: raw.drive_id ?? undefined,
    artifact_id: raw.artifact_id ?? undefined,
    retry_of_job_id: raw.retry_of_job_id ?? undefined,
    state: raw.state,
    progress: Math.max(0, Math.min(100, raw.progress * 100)),
    attempts: raw.attempts,
    max_attempts: raw.max_attempts,
    retryable: raw.retryable ?? undefined,
    error: raw.error ?? undefined,
    result: raw.result,
    available_at: raw.available_at ?? undefined,
    cancel_requested_at: raw.cancel_requested_at ?? undefined,
    created_at: raw.created_at,
    updated_at: raw.updated_at,
    completed_at: raw.completed_at ?? undefined,
  }
}

export function normalizeModel(raw: RawModel, capabilities: RawSimulatorCapabilities): ModelSummary {
  const metadata = raw.metadata ?? {}
  const modelMetadata = metricRecord(metadata, 'model')
  const adapterMetadata = metricRecord(metadata, 'adapter')
  const historySeconds = metricNumber(adapterMetadata, 'history_s')
  const maximumHorizonSeconds = metricNumber(adapterMetadata, 'max_horizon_s')
  const blockedReason = metricString(metadata, 'blocked_reason')
  const reasons = [
    blockedReason ? blockedReason.replaceAll('_', ' ') : undefined,
    metadata.causal_training_eligible === false ? 'causal training provenance is not eligible' : undefined,
    raw.enabled ? undefined : 'model is disabled',
  ].filter((reason): reason is string => Boolean(reason))
  return {
    id: raw.sha256,
    name: raw.name,
    version:
      metricString(modelMetadata, 'artifact', 'version', 'trained_at') ??
      metricString(metadata, 'version', 'trained_at') ??
      raw.sha256.slice(0, 8),
    state: raw.eligible ? 'healthy' : raw.enabled ? 'warning' : 'idle',
    vehicle:
      metricString(adapterMetadata, 'target_car_fingerprint') ??
      metricString(modelMetadata, 'vehicle', 'car_fingerprint') ??
      metricString(metadata, 'vehicle', 'car_fingerprint') ??
      'vehicle-specific',
    horizon_us:
      maximumHorizonSeconds != null
        ? Math.round(maximumHorizonSeconds * 1_000_000)
        : metricNumber(modelMetadata, 'horizon_us') ??
          metricNumber(metadata, 'horizon_us') ??
          capabilities.maximum_horizon_us,
    history_us:
      historySeconds != null
        ? Math.round(historySeconds * 1_000_000)
        : metricNumber(modelMetadata, 'history_us') ??
          metricNumber(metadata, 'history_us') ??
          capabilities.history_required_us,
    hash: raw.sha256,
    mode: raw.mode,
    enabled: raw.enabled,
    eligible: raw.eligible,
    eligibility_reasons: reasons.length ? reasons : raw.eligible ? [] : ['model is not eligible for replay'],
    description:
      metricString(modelMetadata, 'description') ??
      (
        [
          metricString(modelMetadata, 'model_type')?.replaceAll('_', ' '),
          metricNumber(modelMetadata, 'member_count') != null
            ? `${metricNumber(modelMetadata, 'member_count')} ensemble members`
            : undefined,
        ].filter(Boolean).join(' · ') ||
        metricString(metadata, 'description')
      ),
  }
}

export function normalizeParameterSchema(modelId: string, raw: unknown): ParameterSchema {
  const root = raw && typeof raw === 'object' ? raw as Record<string, unknown> : {}
  const list = Array.isArray(raw)
    ? raw
    : Array.isArray(root.parameters)
      ? root.parameters
      : undefined
  if (list) {
    return {
      model_id: modelId,
      title: typeof root.title === 'string' ? root.title : 'Controller parameters',
      description: typeof root.description === 'string' ? root.description : undefined,
      parameters: list.flatMap((entry) => {
        if (!entry || typeof entry !== 'object') return []
        const item = entry as Record<string, unknown>
        const id = String(item.id ?? item.name ?? '')
        if (!id) return []
        const rawType = String(item.type ?? 'number')
        const type: ParameterSchema['parameters'][number]['type'] =
          rawType === 'boolean' || rawType === 'integer' || rawType === 'enum' ? rawType : 'number'
        return [{
          id,
          label: String(item.label ?? id.replaceAll('_', ' ')),
          description: typeof item.description === 'string' ? item.description : undefined,
          group: String(item.group ?? item.category ?? (item.advanced ? 'Advanced' : 'Controller')),
          type,
          unit: typeof item.unit === 'string' ? item.unit : undefined,
          default: (item.default as number | boolean | string | undefined) ?? (type === 'boolean' ? false : 0),
          minimum: typeof item.minimum === 'number' ? item.minimum : typeof item.min === 'number' ? item.min : undefined,
          maximum: typeof item.maximum === 'number' ? item.maximum : typeof item.max === 'number' ? item.max : undefined,
          step: typeof item.step === 'number' ? item.step : undefined,
          scope: typeof item.scope === 'string' ? item.scope : undefined,
          runtime_supported:
            typeof item.runtime_supported === 'boolean'
              ? item.runtime_supported
              : item.scope === 'model_only'
                ? false
                : undefined,
        }]
      }),
    }
  }
  const properties = root.properties && typeof root.properties === 'object'
    ? root.properties as Record<string, Record<string, unknown>>
    : {}
  return {
    model_id: modelId,
    title: typeof root.title === 'string' ? root.title : 'Controller parameters',
    description: typeof root.description === 'string' ? root.description : undefined,
    parameters: Object.entries(properties).map(([id, item]) => ({
      id,
      label: String(item.title ?? id.replaceAll('_', ' ')),
      description: typeof item.description === 'string' ? item.description : undefined,
      group: String(item['x-group'] ?? (item['x-advanced'] === true ? 'Advanced' : 'Controller')),
      type: item.type === 'boolean' ? 'boolean' : item.type === 'integer' ? 'integer' : 'number',
      default: (item.default as number | boolean | string | undefined) ?? 0,
      unit: typeof item['x-units'] === 'string' ? item['x-units'] : undefined,
      minimum: typeof item.minimum === 'number' ? item.minimum : undefined,
      maximum: typeof item.maximum === 'number' ? item.maximum : undefined,
      step: typeof item.multipleOf === 'number' ? item.multipleOf : undefined,
      scope: typeof item['x-scope'] === 'string' ? item['x-scope'] : undefined,
      runtime_supported:
        typeof item['x-runtime-supported'] === 'boolean'
          ? item['x-runtime-supported']
          : item['x-scope'] === 'model_only'
            ? false
            : undefined,
    })),
  }
}

function numericArray(value: unknown): number[] {
  return Array.isArray(value) ? value.filter((item): item is number => typeof item === 'number') : []
}

function nestedRecord(parent: Record<string, unknown>, key: string): Record<string, unknown> {
  const value = parent[key]
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function normalizedSimulationWarning(
  value: unknown,
  index: number,
  limitation = false,
): SimulationResult['warnings'][number] {
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const record = value as Record<string, unknown>
    const severity =
      record.severity === 'blocker' || record.severity === 'warning' || record.severity === 'info'
        ? record.severity
        : limitation
          ? 'info'
          : 'warning'
    const details = Object.fromEntries(
      Object.entries(record).filter(([key]) => !['code', 'severity', 'message'].includes(key)),
    )
    return {
      code: typeof record.code === 'string' ? record.code : `${limitation ? 'limitation' : 'warning'}_${index + 1}`,
      severity,
      message: typeof record.message === 'string' ? record.message : String(value),
      details: Object.keys(details).length ? details : undefined,
    }
  }
  return {
    code: `${limitation ? 'limitation' : 'warning'}_${index + 1}`,
    severity: limitation ? 'info' : 'warning',
    message: String(value),
  }
}

export function normalizeSimulation(raw: RawSimulationView): SimulationResult {
  const payload = raw.result
  const recorded = nestedRecord(payload, 'recorded')
  const baseline = nestedRecord(payload, 'baseline')
  const candidate = nestedRecord(payload, 'candidate')
  const recordedSignals = nestedRecord(recorded, 'signals')
  const baselineSignals = nestedRecord(baseline, 'signals')
  const candidateSignals = nestedRecord(candidate, 'signals')
  const baselineLateral = nestedRecord(baselineSignals, 'actual_lateral_accel')
  const candidateLateral = nestedRecord(candidateSignals, 'actual_lateral_accel')
  const tUs = numericArray(recorded.t_us)
  const points = (values: number[]) => values.slice(0, tUs.length).map((value, index) => ({ t_us: tUs[index], value }))
  const recordedValues = numericArray(recordedSignals.actual_lateral_accel)
  const desiredValues = numericArray(recordedSignals.desired_lateral_accel)
  const baselineValues = numericArray(baselineLateral.mean)
  const candidateValues = numericArray(candidateLateral.mean)
  const baselineStd = numericArray(baselineLateral.std)
  const candidateStd = numericArray(candidateLateral.std)
  const baselineFit = nestedRecord(baseline, 'fit_to_recorded')
  const candidateFit = nestedRecord(candidate, 'fit_to_recorded')
  const baselineMetrics = nestedRecord(baselineFit, 'actual_lateral_accel')
  const candidateMetrics = nestedRecord(candidateFit, 'actual_lateral_accel')
  const warningValues = Array.isArray(payload.warnings) ? payload.warnings : []
  const limitations = Array.isArray(payload.limitations) ? payload.limitations : []
  const warnings = [
    ...warningValues.map((value, index) => normalizedSimulationWarning(value, index)),
    ...limitations.map((value, index) => normalizedSimulationWarning(value, index, true)),
  ]
  const baselineDisagreement = nestedRecord(baseline, 'disagreement')
  const candidateDisagreement = nestedRecord(candidate, 'disagreement')
  const quality = nestedRecord(payload, 'quality')
  const provenance = nestedRecord(payload, 'provenance')
  const modelProvenance = nestedRecord(provenance, 'model')
  const extractorProvenance = nestedRecord(provenance, 'extractor')
  const fullRlog = nestedRecord(provenance, 'full_rlog')
  const sourceObjects = Array.isArray(fullRlog.source_objects) ? fullRlog.source_objects : []
  const rlogHashes = sourceObjects.flatMap((source) => {
    if (!source || typeof source !== 'object') return []
    const record = source as Record<string, unknown>
    return record.log_type === 'rlog' && typeof record.sha256 === 'string'
      ? [record.sha256]
      : []
  })
  const startUs = tUs[0] ?? raw.t_us
  const endUs = tUs.at(-1) ?? raw.t_us + raw.horizon_us
  return {
    id: raw.id,
    state: 'complete',
    start_us: startUs,
    end_us: endUs,
    exact_baseline: payload.exact_baseline === true,
    traces: [
      { id: 'desired', label: 'Desired', unit: 'm/s²', points: points(desiredValues) },
      { id: 'recorded', label: 'Recorded actual', unit: 'm/s²', points: points(recordedValues) },
      { id: 'baseline', label: 'Baseline estimate', unit: 'm/s²', points: points(baselineValues) },
      { id: 'candidate', label: 'Candidate', unit: 'm/s²', points: points(candidateValues) },
    ],
    metrics: ['rmse', 'mae', 'bias', 'p95_abs_error'].flatMap((metric) => {
      const baselineValue = baselineMetrics[metric]
      const candidateValue = candidateMetrics[metric]
      if (typeof baselineValue !== 'number' || typeof candidateValue !== 'number') return []
      return [{
        label: metric.replaceAll('_', ' ').toUpperCase(),
        baseline: baselineValue,
        candidate: candidateValue,
        unit: 'm/s²',
        objective: metric === 'bias' ? 'absolute_lower' as const : 'lower' as const,
      }]
    }),
    warnings,
    uncertainty: {
      baseline_std: points(baselineStd),
      candidate_std: points(candidateStd),
      baseline_disagreement_mean_normalized: metricNumber(baselineDisagreement, 'mean_normalized'),
      baseline_disagreement_p95_normalized: metricNumber(baselineDisagreement, 'p95_normalized'),
      candidate_disagreement_mean_normalized: metricNumber(candidateDisagreement, 'mean_normalized'),
      candidate_disagreement_p95_normalized: metricNumber(candidateDisagreement, 'p95_normalized'),
      maximum_disagreement_p95_normalized: metricNumber(
        quality,
        'max_ensemble_disagreement_p95_normalized',
      ),
    },
    validity: {
      valid: payload.eligible === true,
      flags: [
        payload.eligible === true ? 'eligible' : 'blocked',
        payload.exact_baseline === true ? 'exact baseline' : 'approximate baseline',
        ...warnings
          .filter((warning) => warning.severity !== 'info')
          .map((warning) => warning.code),
      ],
    },
    provenance: {
      model_name:
        metricString(modelProvenance, 'name', 'model_name', 'architecture') ??
        `Model ${raw.model_hash.slice(0, 12)}`,
      model_hash: metricString(provenance, 'model_hash') ?? raw.model_hash,
      controller_mode: String(payload.mode ?? raw.mode),
      starpilot_commit:
        metricString(extractorProvenance, 'source_starpilot_commit', 'starpilot_commit') ??
        'recorded route commit unavailable',
      extractor_version:
        [
          metricString(extractorProvenance, 'extractor'),
          metricString(extractorProvenance, 'extractor_version'),
        ].filter(Boolean).join(' · ') || 'telemetry adapter',
      rlog_hashes: rlogHashes,
      telemetry_hash: metricString(provenance, 'telemetry_ndjson_sha256'),
      timeline_version: metricString(fullRlog, 'timeline_version'),
      input_alignment:
        metricString(provenance, 'input_alignment') ??
        metricString(payload, 'input_alignment'),
    },
  }
}

function parameterSnapshotsEqual(
  left: Record<string, unknown>,
  right: Record<string, number | boolean | string>,
): boolean {
  const leftEntries = Object.entries(left).sort(([leftKey], [rightKey]) => leftKey.localeCompare(rightKey))
  const rightEntries = Object.entries(right).sort(([leftKey], [rightKey]) => leftKey.localeCompare(rightKey))
  return JSON.stringify(leftEntries) === JSON.stringify(rightEntries)
}

async function waitForSimulation(
  id: string,
  expected: SimulationRequest,
  initialJobId: string,
  options: {
    signal?: AbortSignal
    onProgress?: (progress: SimulationProgress) => void
  } = {},
): Promise<SimulationResult> {
  const deadline = Date.now() + 360_000
  let currentJobId = initialJobId
  let canceledJobId: string | undefined
  while (Date.now() < deadline) {
    if (options.signal?.aborted && canceledJobId !== currentJobId) {
      try {
        await request<RawJob>(`/jobs/${encodeURIComponent(currentJobId)}/cancel`, {
          method: 'POST',
        })
        canceledJobId = currentJobId
      } catch {
        // Poll the authoritative simulation state and retry cancellation on the
        // next iteration. Losing a cancel response must not orphan the job.
      }
    }
    const raw = await request<RawSimulationView>(`/simulations/${encodeURIComponent(id)}`)
    if (
      raw.drive_id !== expected.drive_id ||
      raw.model_hash !== expected.model_id ||
      raw.t_us !== expected.start_us ||
      raw.horizon_us !== expected.horizon_us ||
      raw.mode !== 'approximate_closed_loop' ||
      raw.telemetry_sha256 !== expected.telemetry_sha256 ||
      raw.timeline_version !== expected.timeline_version ||
      !parameterSnapshotsEqual(raw.parameters, expected.parameters)
    ) {
      throw new ApiError(
        'The simulation response does not match the submitted request snapshot.',
        409,
        'simulation_snapshot_mismatch',
      )
    }
    currentJobId = raw.job_id
    options.onProgress?.({
      simulation_id: raw.id,
      job_id: raw.job_id,
      state: raw.state,
      progress: Math.max(0, Math.min(1, raw.progress)),
      cancel_requested: options.signal?.aborted === true,
    })
    if (['succeeded', 'complete', 'completed'].includes(raw.state)) {
      if (options.signal?.aborted) {
        throw new ApiError(
          'The obsolete simulation reached a terminal state after cancellation.',
          409,
          'simulation_superseded',
          raw,
        )
      }
      return normalizeSimulation(raw)
    }
    if (['failed', 'rejected', 'expired', 'canceled'].includes(raw.state)) {
      throw new ApiError(raw.error ?? `Simulation ${raw.state}`, 409, `simulation_${raw.state}`, raw)
    }
    await new Promise((resolve) => window.setTimeout(resolve, 750))
  }
  throw new ApiError('Simulation did not finish within the six-minute queue and worker budget.', 408, 'simulation_timeout')
}

async function loadUploads(state?: Upload['state']): Promise<Upload[]> {
  return (await loadUploadPage(state)).items
}

async function loadUploadPage(
  state?: Upload['state'],
  limit = 100,
  offset = 0,
  search?: string,
): Promise<{ items: Upload[]; total: number; limit: number; offset: number }> {
  const rawState =
    state === 'uploading' || state === 'queued' || state === 'paused'
      ? 'receiving'
      : state === 'verifying'
        ? 'finalizing'
      : state === 'ready'
        ? 'complete'
        : state
  const response = await request<{ items: RawUpload[]; total: number }>(
    `/uploads${query({ q: search, limit, offset, state: rawState })}`,
  )
  const normalized = response.items.map(normalizeUpload)
  return {
    items: state ? normalized.filter((upload) => upload.state === state) : normalized,
    total: response.total,
    limit,
    offset,
  }
}

function readinessCounts(raw: Record<string, number>): DriveCatalogPage['summary']['by_readiness'] {
  return {
    importing: raw.importing ?? 0,
    processing: raw.processing ?? 0,
    ready: raw.ready ?? 0,
    partial: raw.partial ?? 0,
    failed: raw.failed ?? 0,
  }
}

async function loadDrives(
  search?: string,
  readiness?: string,
  limit = 50,
  offset = 0,
): Promise<DriveCatalogPage> {
  const raw = await request<RawDriveCatalogPage>(
    `/drives${query({ q: search, readiness, limit, offset })}`,
  )
  return {
    items: raw.items.map(normalizeDrive),
    total: raw.total,
    limit: raw.limit,
    offset: raw.offset,
    summary: {
      duration_us: raw.summary.duration_us,
      stored_bytes: raw.summary.stored_bytes,
      by_readiness: readinessCounts(raw.summary.by_readiness),
    },
  }
}

export const api = {
  async session(): Promise<Session> {
    if (demoMode) return { authenticated: true, username: 'demo' }
    const session = await request<Session>('/auth/me')
    return { ...session, username: session.username ?? 'Administrator' }
  },
  async login(username: string, password: string): Promise<Session> {
    if (demoMode) return { authenticated: true, username }
    const session = await request<Session>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    })
    return { ...session, username }
  },
  async logout(): Promise<void> {
    if (demoMode) return
    return request<void>('/auth/logout', { method: 'POST' })
  },
  async overview(): Promise<Overview> {
    if (demoMode) return (await loadDemoApi()).overview()
    const [raw, receivingUploads, finalizingUploads, drives] = await Promise.all([
      request<RawDashboard>('/dashboard'),
      loadUploadPage('uploading', 8, 0),
      loadUploadPage('verifying', 8, 0),
      loadDrives(undefined, undefined, 6, 0),
    ])
    const workerActive =
      (raw.jobs_by_state.running ?? 0) +
      (raw.jobs_by_state.leased ?? 0) +
      (raw.jobs_by_state.queued ?? 0)
    return {
      generated_at: raw.generated_at,
      devices_online: raw.devices_online,
      devices_total: raw.devices_total,
      upload_bps: raw.upload.bytes_per_second_60s,
      pending_upload_bytes: raw.upload.pending_bytes,
      drives_total: raw.drives_total,
      drives_ready: raw.drives_ready,
      drives_by_readiness: readinessCounts(raw.drives_by_readiness),
      jobs_active: workerActive,
      storage: {
        used_bytes: raw.storage_used_bytes ?? raw.storage_cataloged_bytes,
        capacity_bytes: raw.storage_capacity_bytes ?? undefined,
        free_bytes: raw.storage_free_bytes ?? undefined,
        cataloged_bytes: raw.storage_cataloged_bytes,
        raw_bytes: raw.raw_bytes,
        derived_bytes: raw.derived_bytes,
      },
      services: [
        { id: 'api', label: 'API', state: 'healthy', detail: 'authenticated', updated_at: raw.generated_at },
        {
          id: 'archive',
          label: 'Archive storage',
          state: raw.archive.available && raw.archive.writable ? 'healthy' : 'error',
          detail: !raw.archive.available
            ? 'archive root unavailable'
            : raw.archive.writable
              ? `${raw.artifacts_total} artifacts · writable`
              : 'archive root is not writable',
          updated_at: raw.generated_at,
        },
        {
          id: 'worker',
          label: 'Worker',
          state: raw.worker.online
            ? workerActive ? 'running' : 'healthy'
            : raw.worker.stale ? 'error' : 'offline',
          detail: raw.worker.online
            ? `${workerActive} active or queued`
            : raw.worker.stale
              ? `heartbeat stale · ${workerActive} active or queued`
              : `no heartbeat · ${workerActive} active or queued`,
          updated_at: raw.worker.last_seen ?? raw.generated_at,
        },
        { id: 'ingest', label: 'Ingest', state: raw.upload.failed_uploads ? 'warning' : raw.upload.active_uploads ? 'running' : 'healthy', detail: `${raw.upload.active_uploads} active`, updated_at: raw.generated_at },
      ],
      active_uploads: [...receivingUploads.items, ...finalizingUploads.items]
        .sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at))
        .slice(0, 8),
      recent_drives: drives.items,
    }
  },
  async devices(): Promise<Device[]> {
    if (demoMode) return (await loadDemoApi()).devices()
    return (await request<RawDevice[]>('/devices')).map(normalizeDevice)
  },
  async device(id: string): Promise<Device> {
    if (demoMode) return (await loadDemoApi()).device(id)
    return normalizeDevice(await request<RawDevice>(`/devices/${encodeURIComponent(id)}`))
  },
  async command(id: string, command: Command): Promise<CommandReceipt> {
    return demoMode
      ? (await loadDemoApi()).command(id, command)
      : request<CommandReceipt>(`/devices/${encodeURIComponent(id)}/commands`, {
          method: 'POST',
          body: JSON.stringify(command),
        })
  },
  async commands(id: string, limit = 100): Promise<CommandReceipt[]> {
    return demoMode
      ? []
      : request<CommandReceipt[]>(
          `/devices/${encodeURIComponent(id)}/commands${query({ limit })}`,
        )
  },
  async commandStatus(id: string, commandId: string): Promise<CommandReceipt> {
    if (demoMode) {
      return { id: commandId, state: 'succeeded', message: 'Demo command completed' }
    }
    return request<CommandReceipt>(
      `/devices/${encodeURIComponent(id)}/commands/${encodeURIComponent(commandId)}`,
    )
  },
  async confirmPassword(username: string, password: string): Promise<void> {
    if (demoMode) return
    return request<void>('/auth/confirm', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    })
  },
  async uploads(state?: Upload['state']): Promise<Upload[]> {
    const uploads = demoMode ? await (await loadDemoApi()).uploads() : await loadUploads(state)
    return state ? uploads.filter((upload) => upload.state === state) : uploads
  },
  async uploadPage(
    state?: Upload['state'],
    limit = 100,
    offset = 0,
    search?: string,
  ): Promise<{ items: Upload[]; total: number; limit: number; offset: number }> {
    if (!demoMode) return loadUploadPage(state, limit, offset, search)
    const uploads = await (await loadDemoApi()).uploads()
    const needle = search?.trim().toLowerCase()
    const filtered = uploads.filter((upload) =>
      (!state || upload.state === state) &&
      (!needle || `${upload.filename} ${upload.device_name ?? ''} ${upload.route_id ?? ''} ${upload.device_id}`.toLowerCase().includes(needle)))
    return {
      items: filtered.slice(offset, offset + limit),
      total: filtered.length,
      limit,
      offset,
    }
  },
  async uploadSnapshot(): Promise<UploadSnapshot> {
    if (demoMode) {
      const uploads = await (await loadDemoApi()).uploads()
      const active = uploads.filter((upload) => upload.state === 'uploading')
      return {
        active_uploads: active.length,
        failed_uploads: uploads.filter((upload) => upload.state === 'failed').length,
        completed_uploads: uploads.filter((upload) => upload.state === 'ready').length,
        bytes_received: uploads.reduce((sum, upload) => sum + upload.received_bytes, 0),
        bytes_expected: uploads.reduce((sum, upload) => sum + upload.size_bytes, 0),
        pending_bytes: active.reduce(
          (sum, upload) => sum + Math.max(0, upload.size_bytes - upload.received_bytes),
          0,
        ),
        bytes_per_second_60s: active.reduce((sum, upload) => sum + (upload.upload_bps ?? 0), 0),
        by_device: {},
      }
    }
    return request<UploadSnapshot>('/uploads/snapshot')
  },
  async jobCounts(): Promise<Record<string, number>> {
    if (demoMode) return {}
    return request<RawDashboard>('/dashboard').then((dashboard) => dashboard.jobs_by_state)
  },
  async jobs(filters: {
    state?: Job['state']
    type?: string
    uploadId?: string
    driveId?: string
    q?: string
    limit?: number
    offset?: number
  } = {}): Promise<{ items: Job[]; total: number }> {
    if (demoMode) {
      const page = await (await loadDemoApi()).jobs()
      const needle = filters.q?.trim().toLowerCase()
      const filtered = page.items.filter((job) =>
        (!filters.state || job.state === filters.state) &&
        (!filters.type || job.type === filters.type) &&
        (!filters.uploadId || job.upload_id === filters.uploadId) &&
        (!filters.driveId || job.drive_id === filters.driveId) &&
        (!needle || `${job.id} ${job.type} ${job.error ?? ''} ${job.upload_id ?? ''} ${job.drive_id ?? ''} ${job.artifact_id ?? ''}`.toLowerCase().includes(needle)))
      const offset = filters.offset ?? 0
      const limit = filters.limit ?? 100
      return {
        items: filtered.slice(offset, offset + limit),
        total: filtered.length,
      }
    }
    const response = await request<{ items: RawJob[]; total: number }>(
      `/jobs${query({
        state: filters.state,
        type: filters.type,
        upload_id: filters.uploadId,
        drive_id: filters.driveId,
        q: filters.q,
        limit: filters.limit ?? 100,
        offset: filters.offset ?? 0,
      })}`,
    )
    return { items: response.items.map(normalizeJob), total: response.total }
  },
  async cancelJob(id: string): Promise<Job> {
    if (demoMode) throw new ApiError('Demo jobs cannot be canceled.', 409, 'demo_job_control_unavailable')
    return request<RawJob>(`/jobs/${encodeURIComponent(id)}/cancel`, {
      method: 'POST',
    }).then(normalizeJob)
  },
  async retryJob(id: string): Promise<Job> {
    if (demoMode) throw new ApiError('Demo jobs cannot be retried.', 409, 'demo_job_control_unavailable')
    return request<RawJob>(`/jobs/${encodeURIComponent(id)}/retry`, {
      method: 'POST',
    }).then(normalizeJob)
  },
  async drives(
    search?: string,
    readiness?: string,
    limit = 50,
    offset = 0,
  ): Promise<DriveCatalogPage> {
    return demoMode
      ? (await loadDemoApi()).drives(search, readiness, limit, offset)
      : loadDrives(search, readiness, limit, offset)
  },
  async drive(id: string): Promise<DriveDetail> {
    if (demoMode) return (await loadDemoApi()).drive(id)
    return normalizeDriveDetail(await request<RawDriveDetail>(`/drives/${encodeURIComponent(id)}`))
  },
  async series(
    id: string,
    startUs: number,
    endUs: number,
    signals: string[],
    maxPoints = 1_800,
    generation?: {
      telemetry_sha256: string
      timeline_version: string
    },
    maxMarkers = 1_000,
  ): Promise<DriveSeries> {
    return demoMode
      ? (await loadDemoApi()).series()
      : request<RawSeries>(
          `/drives/${encodeURIComponent(id)}/series${query({
            start_us: Math.round(startUs),
            end_us: Math.round(endUs),
            signals: signals.join(','),
            max_points: maxPoints,
            max_markers: maxMarkers,
            telemetry_sha256: generation?.telemetry_sha256,
            timeline_version: generation?.timeline_version,
          })}`,
        ).then((raw) => normalizeSeries(raw, generation
          ? {
              drive_id: id,
              telemetry_sha256: generation.telemetry_sha256,
              timeline_version: generation.timeline_version,
            }
          : undefined))
  },
  async mediaManifest(id: string, camera: string): Promise<MediaManifest> {
    return demoMode
      ? (await loadDemoApi()).mediaManifest(id, camera)
      : request<MediaManifest>(`/drives/${encodeURIComponent(id)}/media-manifest${query({ camera })}`)
  },
  async mediaSync(syncUrl: string): Promise<MediaSyncIndex> {
    if (demoMode) throw new ApiError('Demo mode has no exact media timeline.', 404, 'demo_media_sync_unavailable')
    return request<MediaSyncIndex>(apiRelativePath(syncUrl))
  },
  async models(): Promise<ModelSummary[]> {
    if (demoMode) return (await loadDemoApi()).models()
    const capabilities = await request<RawSimulatorCapabilities>('/simulator/capabilities')
    return capabilities.models.map((model) => normalizeModel(model, capabilities))
  },
  async parameterSchema(modelId: string): Promise<ParameterSchema> {
    return demoMode
      ? (await loadDemoApi()).parameterSchema()
      : request<unknown>(`/models/${encodeURIComponent(modelId)}/parameters`)
          .then((raw) => normalizeParameterSchema(modelId, raw))
  },
  async simulate(
    requestBody: SimulationRequest,
    options: {
      signal?: AbortSignal
      onProgress?: (progress: SimulationProgress) => void
    } = {},
  ): Promise<SimulationResult> {
    if (demoMode) {
      options.onProgress?.({
        simulation_id: 'demo-pending',
        job_id: 'demo-job',
        state: 'running',
        progress: 0.5,
        cancel_requested: false,
      })
      const result = await (await loadDemoApi()).simulate(requestBody)
      if (options.signal?.aborted) {
        throw new ApiError('Demo simulation canceled.', 409, 'simulation_canceled')
      }
      return result
    }
    const accepted = await request<RawSimulationAccepted>(
      `/drives/${encodeURIComponent(requestBody.drive_id)}/simulations`,
      {
        method: 'POST',
        body: JSON.stringify({
          t_us: requestBody.start_us,
          horizon_us: requestBody.horizon_us,
          model_hash: requestBody.model_id,
          telemetry_sha256: requestBody.telemetry_sha256,
          timeline_version: requestBody.timeline_version,
          mode: 'approximate_closed_loop',
          parameters: requestBody.parameters,
        }),
      },
    )
    options.onProgress?.({
      simulation_id: accepted.id,
      job_id: accepted.job_id,
      state: accepted.state,
      progress: 0,
      cancel_requested: options.signal?.aborted === true,
    })
    return waitForSimulation(
      accepted.id,
      requestBody,
      accepted.job_id,
      options,
    )
  },
  async activity(): Promise<ActivityItem[]> {
    if (demoMode) return (await loadDemoApi()).activity()
    const raw = await request<RawAuditEvent[]>('/audit-events?limit=200')
    return raw.map((event) => ({
      id: String(event.id),
      created_at: event.created_at,
      actor: event.actor_id ? `${event.actor_type}:${event.actor_id}` : event.actor_type,
      action: event.action,
      target: [event.resource_type, event.resource_id].filter(Boolean).join(':') || 'system',
      state: event.action.includes('fail') || event.action.includes('reject') ? 'error' : event.action.includes('start') ? 'running' : 'healthy',
      detail: typeof event.details.message === 'string'
        ? event.details.message
        : Object.keys(event.details).length
          ? Object.entries(event.details).slice(0, 2).map(([key, value]) => `${key}=${String(value)}`).join(' · ')
          : undefined,
    }))
  },
  async settings(): Promise<Settings> {
    return demoMode ? (await loadDemoApi()).settings() : request<Settings>('/settings')
  },
  async saveSettings(settings: Settings): Promise<Settings> {
    if (demoMode) return settings
    return request<Settings>('/settings', {
      method: 'PATCH',
      body: JSON.stringify(settings),
    })
  },
}
