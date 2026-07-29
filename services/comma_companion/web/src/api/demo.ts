import type {
  ActivityItem,
  Command,
  CommandReceipt,
  Device,
  Drive,
  DriveCatalogPage,
  DriveDetail,
  DriveSeries,
  InboundStatus,
  Job,
  ModelSummary,
  MediaManifest,
  Overview,
  ParameterSchema,
  Settings,
  SimulationRequest,
  SimulationResult,
  Upload,
} from './types'

const now = Date.now()
const isoAgo = (minutes: number) => new Date(now - minutes * 60_000).toISOString()

export const demoDevices: Device[] = [
  {
    id: 'device-ioniq5',
    name: 'Ioniq 5',
    dongle_id: 'fe7070223b',
    state: 'healthy',
    online: true,
    onroad: false,
    offroad: true,
    last_seen_at: isoAgo(0.2),
    started_at: isoAgo(880),
    software_version: 'StarPilot 2026.07.28',
    git_branch: 'Starpilot-20inch-ioniq5',
    git_commit: '19f8c767',
    vehicle: '2022 Hyundai Ioniq 5',
    ip_address: '100.98.247.60',
    network_type: 'Wi-Fi',
    network_metered: false,
    upload_bps: 5_900_000,
    queue_bytes: 18_420_000_000,
    spool_bytes: 21_200_000_000,
    spool_capacity_bytes: 231_000_000_000,
    free_space_bytes: 91_700_000_000,
    temperature_c: 44.2,
    battery_percent: 83,
    capabilities: [
      'resumable_upload_v1',
      'typed_commands_v1',
      'command_status',
      'command_rescan',
      'command_pause',
      'command_resume',
      'command_restart_agent',
    ],
  },
]

export const demoUploads: Upload[] = [
  {
    id: 'upload-1',
    device_id: 'device-ioniq5',
    device_name: 'Ioniq 5',
    route_id: 'drive-current',
    segment: 8,
    filename: 'fcamera.hevc',
    kind: 'road_camera',
    size_bytes: 78_420_000,
    received_bytes: 51_810_000,
    state: 'uploading',
    upload_bps: 5_900_000,
    eta_seconds: 5,
    created_at: isoAgo(1.2),
    updated_at: isoAgo(0.02),
  },
  {
    id: 'upload-2',
    device_id: 'device-ioniq5',
    device_name: 'Ioniq 5',
    route_id: 'drive-current',
    segment: 8,
    filename: 'rlog.zst',
    kind: 'rlog',
    size_bytes: 13_520_000,
    received_bytes: 13_520_000,
    state: 'ready',
    created_at: isoAgo(1.5),
    updated_at: isoAgo(0.4),
  },
  {
    id: 'upload-3',
    device_id: 'device-ioniq5',
    device_name: 'Ioniq 5',
    route_id: 'drive-current',
    segment: 7,
    filename: 'ecamera.hevc',
    kind: 'wide_camera',
    size_bytes: 73_100_000,
    received_bytes: 73_100_000,
    state: 'ready',
    created_at: isoAgo(3),
    updated_at: isoAgo(0.6),
  },
]

export const demoJobs: Job[] = [
  {
    id: 'job-transcode-wide-7',
    type: 'transcode_video',
    label: 'transcode video',
    state: 'running',
    progress: 41,
    attempts: 1,
    max_attempts: 3,
    result: {},
    created_at: isoAgo(3),
    updated_at: isoAgo(0.6),
  },
  {
    id: 'job-index-rlog-8',
    type: 'extract_telemetry',
    label: 'extract telemetry',
    state: 'queued',
    progress: 0,
    attempts: 0,
    max_attempts: 3,
    result: {},
    created_at: isoAgo(1.5),
    updated_at: isoAgo(1.5),
  },
]

export const demoDrives: Drive[] = [
  {
    id: 'drive-current',
    device_id: 'device-ioniq5',
    route_name: '0000011c--116efaac12',
    started_at: isoAgo(38),
    ended_at: isoAgo(4),
    duration_us: 2_045_000_000,
    distance_m: 24_830,
    readiness: 'processing',
    segment_count: 9,
    ready_segments: 7,
    expected_media: 18,
    ready_media: 14,
    missing_media: 4,
    failed_media: 0,
    pruned_media: 14,
    raw_video_pruning_required: true,
    backup_bytes_received: 1_738_000_000,
    backup_bytes_expected: 1_950_000_000,
    artifact_count: 48,
    cameras: [
      { id: 'road', label: 'Road', available: true, codec: 'AV1', width: 1928, height: 1208, fps: 20 },
      { id: 'wide', label: 'Wide', available: true, codec: 'AV1', width: 1928, height: 1208, fps: 20 },
      { id: 'driver', label: 'Driver', available: false },
    ],
    telemetry_ready: true,
    vehicle: '2022 Hyundai Ioniq 5',
    software_version: '19f8c767',
    model_name: 'north-dakota v2',
    location_start: 'Fjellhamar',
    location_end: 'Oslo',
    raw_bytes: 1_420_000_000,
    derived_bytes: 318_000_000,
    flags: ['driver overlay · 2', 'hard brake · 1'],
  },
  {
    id: 'drive-2',
    device_id: 'device-ioniq5',
    route_name: '0000011b--8d2dd3a9c4',
    started_at: isoAgo(1_463),
    ended_at: isoAgo(1_418),
    duration_us: 2_694_000_000,
    distance_m: 48_200,
    readiness: 'ready',
    segment_count: 45,
    ready_segments: 45,
    expected_media: 90,
    ready_media: 90,
    missing_media: 0,
    failed_media: 0,
    pruned_media: 90,
    raw_video_pruning_required: true,
    backup_bytes_received: 8_970_000_000,
    backup_bytes_expected: 8_970_000_000,
    artifact_count: 228,
    cameras: [
      { id: 'road', label: 'Road', available: true, codec: 'AV1', width: 1928, height: 1208, fps: 20 },
      { id: 'wide', label: 'Wide', available: true, codec: 'AV1', width: 1928, height: 1208, fps: 20 },
    ],
    telemetry_ready: true,
    vehicle: '2022 Hyundai Ioniq 5',
    software_version: '19f8c767',
    model_name: 'north-dakota v2',
    location_start: 'Oslo',
    location_end: 'Drammen',
    raw_bytes: 7_430_000_000,
    derived_bytes: 1_610_000_000,
  },
  {
    id: 'drive-3',
    device_id: 'device-ioniq5',
    route_name: '0000011a--04df9cd3f8',
    started_at: isoAgo(2_997),
    ended_at: isoAgo(2_985),
    duration_us: 718_000_000,
    distance_m: 8_680,
    readiness: 'partial',
    segment_count: 12,
    ready_segments: 11,
    expected_media: 12,
    ready_media: 11,
    missing_media: 1,
    failed_media: 0,
    pruned_media: 11,
    raw_video_pruning_required: true,
    backup_bytes_received: 2_271_000_000,
    backup_bytes_expected: 2_400_000_000,
    artifact_count: 42,
    cameras: [{ id: 'road', label: 'Road', available: true, codec: 'AV1', width: 1928, height: 1208, fps: 20 }],
    telemetry_ready: true,
    vehicle: '2022 Hyundai Ioniq 5',
    software_version: '19f8c767',
    model_name: 'north-dakota v2',
    location_start: 'Lørenskog',
    location_end: 'Oslo',
    raw_bytes: 1_870_000_000,
    derived_bytes: 401_000_000,
    flags: ['wide camera missing · segment 11'],
  },
]

export const demoModels: ModelSummary[] = [
  {
    id: 'ioniq5-neural-lateral-20260723',
    name: 'Ioniq 5 neural lateral plant',
    version: '2026.07.23',
    state: 'healthy',
    vehicle: 'HYUNDAI_IONIQ_5',
    horizon_us: 2_000_000,
    history_us: 3_000_000,
    hash: 'fb1b8b951fdff19ff5f9349470415d003b61ee5655fff996365b429d93f6dc45',
    mode: 'approximate_closed_loop',
    enabled: true,
    eligible: true,
    eligibility_reasons: [],
    last_used_at: isoAgo(16),
    description: 'Three-member GRU ensemble trained on clean Ioniq 5 lateral-control windows.',
  },
]

const seriesPointCount = 760
const seriesStart = 0
const seriesEnd = 2_045_000_000

function buildPoints(fn: (t: number, index: number) => number): Array<{ t_us: number; value: number }> {
  return Array.from({ length: seriesPointCount }, (_, index) => {
    const t = index / (seriesPointCount - 1)
    return { t_us: Math.round(seriesStart + t * seriesEnd), value: fn(t, index) }
  })
}

export const demoSeries: DriveSeries = {
  drive_id: 'drive-current',
  ndjson_sha256: 'a'.repeat(64),
  timeline_version: 'b'.repeat(64),
  timeline_origin: 'stable',
  start_us: seriesStart,
  end_us: seriesEnd,
  signals: [
    {
      id: 'desired_lateral_accel',
      label: 'Desired lateral accel',
      unit: 'm/s²',
      color: '#86efc2',
      points: buildPoints((t) => 1.28 * Math.sin(t * 31) + 0.22 * Math.sin(t * 111)),
    },
    {
      id: 'actual_lateral_accel',
      label: 'Actual lateral accel',
      unit: 'm/s²',
      color: '#63b3ff',
      points: buildPoints((t) => 1.14 * Math.sin(t * 31 - 0.17) + 0.17 * Math.sin(t * 111 - 0.3)),
    },
    {
      id: 'applied_torque',
      label: 'Applied torque',
      unit: 'normalized',
      color: '#f4bb62',
      points: buildPoints((t) => 0.35 * Math.sin(t * 31 + 0.2) + 0.07 * Math.sin(t * 80)),
    },
    {
      id: 'steering_angle',
      label: 'Steering angle',
      unit: 'deg',
      color: '#c39cff',
      points: buildPoints((t) => 14 * Math.sin(t * 15.5) + 3.2 * Math.sin(t * 41)),
    },
    {
      id: 'v_ego',
      label: 'Speed',
      unit: 'm/s',
      color: '#9ba8b0',
      points: buildPoints((t) => 21 + 6 * Math.sin(t * 4.5) - (t > 0.72 && t < 0.77 ? 7 : 0)),
    },
  ],
  events: [
    { id: 'event-active-1', lane: 'active', start_us: 42_000_000, end_us: 611_000_000, label: 'Lateral active' },
    { id: 'event-active-2', lane: 'active', start_us: 663_000_000, end_us: 1_704_000_000, label: 'Lateral active' },
    { id: 'event-overlay-1', lane: 'driver_overlay', start_us: 377_000_000, end_us: 391_000_000, label: 'Driver torque', severity: 'warning' },
    { id: 'event-overlay-2', lane: 'driver_overlay', start_us: 1_284_000_000, end_us: 1_306_000_000, label: 'Driver torque', severity: 'warning' },
    { id: 'event-alert-1', lane: 'alert', start_us: 1_514_000_000, end_us: 1_520_000_000, label: 'Hard brake', severity: 'critical' },
    { id: 'event-sat-1', lane: 'saturation', start_us: 932_000_000, end_us: 946_000_000, label: 'Torque saturation', severity: 'warning' },
  ],
  gaps: [{ start_us: 1_805_000_000, end_us: 1_866_000_000, reason: 'Segment 8 video still processing' }],
  markers_truncated: false,
}

export const demoDriveDetail: DriveDetail = {
  ...demoDrives[0],
  segments: Array.from({ length: 9 }, (_, index) => ({
    index,
    start_us: index * 240_000_000,
    end_us: Math.min((index + 1) * 240_000_000, seriesEnd),
    state: index < 7 ? 'ready' : index === 7 ? 'processing' : 'uploading',
    missing: index === 8 ? ['road video'] : undefined,
    expected_streams: [],
  })),
  telemetry_start_us: seriesStart,
  telemetry_end_us: seriesEnd,
  rlog_hashes: ['1af893f6…74ac', '2b19ced0…d2f1'],
  telemetry_generation: {
    state: 'complete',
    schema_version: 1,
    ndjson_sha256: demoSeries.ndjson_sha256,
    source_fingerprint: 'c'.repeat(64),
    timeline_version: demoSeries.timeline_version,
    publication_ready: true,
    start_t_us: seriesStart,
    end_t_us: seriesEnd,
    extractor: 'comma-companion-rlog',
    extractor_version: 'demo',
    source_starpilot_commit: '19f8c767',
    source_rlogs: [],
    vehicle: {},
    route_software: {},
    completeness: {},
  },
  simulation_eligible: true,
  simulation_eligibility_reasons: [],
}

export const demoParameterSchema: ParameterSchema = {
  model_id: demoModels[0].id,
  title: 'Lateral controller tune',
  description: 'Candidate values are evaluated against the archived window only. Nothing is written to the car.',
  parameters: [
    {
      id: 'latAccelFactor',
      label: 'Lateral accel factor',
      description: 'Scales the torque required per unit lateral acceleration.',
      group: 'Torque curve',
      type: 'number',
      unit: 'Nm/(m/s²)',
      default: 2.7,
      minimum: 1.5,
      maximum: 4.2,
      step: 0.05,
    },
    {
      id: 'friction',
      label: 'Friction compensation',
      description: 'Static steering-system friction offset.',
      group: 'Torque curve',
      type: 'number',
      unit: 'Nm',
      default: 0.105,
      minimum: 0.02,
      maximum: 0.24,
      step: 0.005,
    },
    {
      id: 'kp',
      label: 'Proportional gain',
      group: 'Feedback',
      type: 'number',
      default: 1.0,
      minimum: 0.4,
      maximum: 2.0,
      step: 0.02,
    },
    {
      id: 'ki',
      label: 'Integral gain',
      group: 'Feedback',
      type: 'number',
      default: 0.1,
      minimum: 0,
      maximum: 0.4,
      step: 0.005,
    },
    {
      id: 'unwindTaper',
      label: 'Unwind taper',
      group: 'Transitions',
      type: 'number',
      default: 0.72,
      minimum: 0.35,
      maximum: 1,
      step: 0.01,
    },
  ],
}

export const demoActivity: ActivityItem[] = [
  { id: 'a1', created_at: isoAgo(0.2), actor: 'device:Ioniq 5', action: 'heartbeat', target: 'Ioniq 5', state: 'healthy', detail: 'Parked · Wi-Fi' },
  { id: 'a2', created_at: isoAgo(0.4), actor: 'worker', action: 'verified artifact', target: 'segment 8 / rlog.zst', state: 'healthy', detail: 'SHA-256 matched' },
  { id: 'a3', created_at: isoAgo(0.6), actor: 'worker', action: 'transcoding', target: 'segment 7 / wide camera', state: 'running', detail: 'AV1 · 41%' },
  { id: 'a4', created_at: isoAgo(16), actor: 'danielv', action: 'ran simulation', target: demoDrives[1].route_name, state: 'healthy', detail: '1.0 s horizon' },
  { id: 'a5', created_at: isoAgo(91), actor: 'device:Ioniq 5', action: 'command accepted', target: 'pause_uploads', state: 'healthy' },
]

export const demoSettings: Settings = {
  archive_path: '/archive/comma-companion',
  raw_log_retention_enabled: true,
  raw_video_retention_enabled: false,
  transcode_codec: 'av1',
  transcode_crf: 40,
  worker_concurrency: 1,
  metered_uploads_allowed: false,
  timezone: 'Europe/Oslo',
}

export const demoOverview: Overview = {
  generated_at: new Date(now).toISOString(),
  devices_online: 1,
  devices_total: 1,
  upload_bps: 5_900_000,
  pending_upload_bytes: 18_420_000_000,
  drives_total: 68,
  drives_ready: 64,
  drives_by_readiness: {
    importing: 0,
    processing: 2,
    ready: 64,
    partial: 2,
    failed: 0,
  },
  jobs_active: 2,
  worker_transcode_jobs_remaining: 5,
  worker_eta_seconds: 315,
  storage: {
    used_bytes: 178_000_000_000,
    capacity_bytes: 72_000_000_000_000,
    free_bytes: 71_822_000_000_000,
    cataloged_bytes: 178_000_000_000,
    raw_bytes: 154_000_000_000,
    derived_bytes: 24_000_000_000,
  },
  services: [
    { id: 'api', label: 'API', state: 'healthy', detail: '18 ms', updated_at: isoAgo(0.1) },
    { id: 'archive', label: 'Archive', state: 'healthy', detail: '72 TB free', updated_at: isoAgo(0.1) },
    { id: 'worker', label: 'AV1 worker', state: 'running', detail: '5 encodes remaining · ETA 6 min', updated_at: isoAgo(0.3) },
    { id: 'telemetry', label: 'Telemetry index', state: 'healthy', detail: 'current', updated_at: isoAgo(0.4) },
  ],
  active_uploads: demoUploads,
  recent_drives: demoDrives,
}

function runDemoSimulation(request: SimulationRequest): SimulationResult {
  const count = 101
  const end = request.start_us + request.horizon_us
  const tuningDelta = Object.values(request.parameters).reduce<number>(
    (sum, value) => sum + (typeof value === 'number' ? value : 0),
    0,
  )
  const points = (lag: number, scale: number) =>
    Array.from({ length: count }, (_, index) => {
      const progress = index / (count - 1)
      const t_us = Math.round(request.start_us + progress * request.horizon_us)
      const phase = progress * 7.4
      return { t_us, value: scale * (0.88 * Math.sin(phase - lag) + 0.23 * Math.sin(phase * 2.4 - lag)) }
    })
  return {
    id: `simulation-${Date.now()}`,
    state: 'complete',
    start_us: request.start_us,
    end_us: end,
    exact_baseline: false,
    traces: [
      { id: 'desired', label: 'Desired', unit: 'm/s²', points: points(0, 1.2) },
      { id: 'recorded', label: 'Recorded actual', unit: 'm/s²', points: points(0.28, 1.08) },
      { id: 'baseline', label: 'Baseline estimate', unit: 'm/s²', points: points(0.25, 1.06) },
      { id: 'candidate', label: 'Candidate', unit: 'm/s²', points: points(0.18, 1.04 + (tuningDelta % 4) * 0.01) },
    ],
    metrics: [
      { label: 'Tracking RMSE', baseline: 0.184, candidate: 0.139, unit: 'm/s²', objective: 'lower' },
      { label: 'Peak error', baseline: 0.391, candidate: 0.312, unit: 'm/s²', objective: 'lower' },
      { label: 'Bias', baseline: -0.09, candidate: -0.03, unit: 'm/s²', objective: 'absolute_lower' },
    ],
    warnings: [{
      code: 'read_only_estimate',
      severity: 'info',
      message: 'Candidate response is a model estimate, not a command sent to the car.',
    }],
    uncertainty: {
      baseline_std: points(0, 0).map((point) => ({ ...point, value: 0.035 })),
      candidate_std: points(0, 0).map((point) => ({ ...point, value: 0.028 })),
      baseline_disagreement_p95_normalized: 0.16,
      candidate_disagreement_p95_normalized: 0.12,
      maximum_disagreement_p95_normalized: 0.16,
    },
    validity: { valid: true, flags: ['clean history', 'lateral active', 'in distribution'] },
    provenance: {
      model_name: demoModels[0].name,
      model_hash: demoModels[0].hash,
      controller_mode: demoModels[0].mode,
      starpilot_commit: '19f8c767',
      extractor_version: 'rlog-v1',
      rlog_hashes: demoDriveDetail.rlog_hashes ?? [],
    },
  }
}

export const demoApi = {
  async overview(): Promise<Overview> {
    return demoOverview
  },
  async inbound(): Promise<InboundStatus> {
    return {
      generated_at: new Date().toISOString(),
      devices_online: demoDevices.filter((device) => device.online).length,
      devices_total: demoDevices.length,
      upload_bps: demoOverview.upload_bps,
      pending_upload_bytes: Math.max(
        demoOverview.pending_upload_bytes,
        demoDevices.reduce((total, device) => total + (device.queue_bytes ?? 0), 0),
      ),
      server_pending_bytes: demoOverview.pending_upload_bytes,
      bytes_received: demoUploads.reduce((total, upload) => total + upload.received_bytes, 0),
    }
  },
  async devices(): Promise<Device[]> {
    return demoDevices
  },
  async device(id: string): Promise<Device> {
    return demoDevices.find((device) => device.id === id) ?? demoDevices[0]
  },
  async command(_id: string, command: Command): Promise<CommandReceipt> {
    return { id: `command-${Date.now()}`, state: 'queued', message: `${command.type} queued in demo mode` }
  },
  async uploads(): Promise<Upload[]> {
    return demoUploads
  },
  async jobs(): Promise<{ items: Job[]; total: number }> {
    return { items: demoJobs, total: demoJobs.length }
  },
  async drives(
    search?: string,
    readiness?: string,
    limit = 50,
    offset = 0,
  ): Promise<DriveCatalogPage> {
    const needle = search?.trim().toLowerCase()
    const filtered = demoDrives.filter((drive) =>
      (!needle || [drive.route_name, drive.device_id, drive.vehicle]
        .some((value) => value?.toLowerCase().includes(needle))) &&
      (!readiness || drive.readiness === readiness))
    const byReadiness: DriveCatalogPage['summary']['by_readiness'] = {
      importing: 0,
      processing: 0,
      ready: 0,
      partial: 0,
      failed: 0,
    }
    filtered.forEach((drive) => {
      if (drive.readiness !== 'uploading') byReadiness[drive.readiness] += 1
    })
    return {
      items: filtered.slice(offset, offset + limit),
      total: filtered.length,
      limit,
      offset,
      summary: {
        duration_us: filtered.reduce((sum, drive) => sum + drive.duration_us, 0),
        stored_bytes: filtered.reduce(
          (sum, drive) => sum + (drive.stored_bytes ?? 0) +
            (drive.stored_bytes == null ? (drive.raw_bytes ?? 0) + (drive.derived_bytes ?? 0) : 0),
          0,
        ),
        by_readiness: byReadiness,
      },
    }
  },
  async drive(id: string): Promise<DriveDetail> {
    if (id === demoDriveDetail.id) return demoDriveDetail
    const drive = demoDrives.find((item) => item.id === id) ?? demoDrives[0]
    return {
      ...drive,
      segments: Array.from({ length: drive.segment_count }, (_, index) => ({
        index,
        start_us: index * 60_000_000,
        end_us: Math.min((index + 1) * 60_000_000, drive.duration_us),
        state: index < drive.ready_segments ? 'ready' : drive.readiness,
        expected_streams: [],
      })),
      simulation_eligible: false,
      simulation_eligibility_reasons: [{
        code: 'demo_drive_not_modeled',
        message: 'This demo drive has no matching dynamics model.',
      }],
    }
  },
  async mediaManifest(id: string, camera: string): Promise<MediaManifest> {
    return { drive_id: id, camera, synchronized: false, items: [] }
  },
  async series(): Promise<DriveSeries> {
    return demoSeries
  },
  async models(): Promise<ModelSummary[]> {
    return demoModels
  },
  async parameterSchema(): Promise<ParameterSchema> {
    return demoParameterSchema
  },
  async simulate(request: SimulationRequest): Promise<SimulationResult> {
    await new Promise((resolve) => window.setTimeout(resolve, 480))
    return runDemoSimulation(request)
  },
  async activity(): Promise<ActivityItem[]> {
    return demoActivity
  },
  async settings(): Promise<Settings> {
    return demoSettings
  },
}
