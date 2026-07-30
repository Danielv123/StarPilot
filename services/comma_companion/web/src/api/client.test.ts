import { describe, expect, it, vi } from 'vitest'
import {
  ApiError,
  api,
  isSessionExpiryError,
  normalizeDevice,
  normalizeDriveDetail,
  normalizeModel,
  normalizeParameterSchema,
  normalizeSeries,
  normalizeSimulation,
  sessionExpiredEvent,
} from './client'

describe('session expiry handling', () => {
  it('broadcasts only server-confirmed session expiry errors', async () => {
    let expiryEvents = 0
    const onExpiry = () => { expiryEvents += 1 }
    window.addEventListener(sessionExpiredEvent, onExpiry)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response(
      JSON.stringify({
        error: {
          code: 'session_expired',
          message: 'Administrator session expired',
        },
      }),
      {
        status: 401,
        headers: { 'content-type': 'application/json' },
      },
    )))
    try {
      await expect(api.session()).rejects.toSatisfy(isSessionExpiryError)
      expect(expiryEvents).toBe(1)
      expect(isSessionExpiryError(new ApiError('No', 401, 'invalid_password'))).toBe(false)
    } finally {
      window.removeEventListener(sessionExpiredEvent, onExpiry)
      vi.unstubAllGlobals()
    }
  })
})

describe('command status lookup', () => {
  it('uses the command-by-id endpoint instead of a bounded recent-command list', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      id: 'command?1',
      device_id: 'comma/1',
      type: 'rescan',
      state: 'running',
      issued_at: '2026-01-01T00:00:00Z',
      expires_at: '2026-01-01T00:02:00Z',
    }), { status: 200, headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)

    try {
      await expect(api.commandStatus('comma/1', 'command?1')).resolves.toMatchObject({
        id: 'command?1',
        state: 'running',
      })
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/v1/devices/comma%2F1/commands/command%3F1',
        expect.anything(),
      )
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('simulation response normalization', () => {
  it('preserves nested provenance, warnings, uncertainty, and signed-bias semantics', () => {
    const result = normalizeSimulation({
      id: 'simulation-1',
      drive_id: 'drive-1',
      job_id: 'job-1',
      t_us: 4_000_000,
      horizon_us: 100_000,
      model_hash: 'a'.repeat(64),
      mode: 'approximate_closed_loop',
      parameters: { damping_gain: 0.02 },
      telemetry_sha256: 'b'.repeat(64),
      timeline_version: 'e'.repeat(64),
      state: 'succeeded',
      progress: 1,
      error: null,
      result: {
        mode: 'approximate_closed_loop',
        exact_baseline: false,
        eligible: false,
        input_alignment: 'timestamp_causal_recorded_history_asof',
        warnings: [{
          code: 'model_disagreement',
          severity: 'blocker',
          message: 'Disagreement exceeded the confidence gate.',
          p95_normalized: 0.61,
        }],
        limitations: ['Read-only estimate.'],
        recorded: {
          t_us: [4_010_000, 4_020_000],
          signals: {
            desired_lateral_accel: [0.1, 0.2],
            actual_lateral_accel: [0.08, 0.18],
          },
        },
        baseline: {
          signals: { actual_lateral_accel: { mean: [0.07, 0.17], std: [0.01, 0.02] } },
          disagreement: { mean_normalized: 0.1, p95_normalized: 0.2 },
          fit_to_recorded: { actual_lateral_accel: { rmse: 0.02, mae: 0.01, bias: -0.01, p95_abs_error: 0.03 } },
        },
        candidate: {
          signals: { actual_lateral_accel: { mean: [0.08, 0.18], std: [0.03, 0.04] } },
          disagreement: { mean_normalized: 0.3, p95_normalized: 0.61 },
          fit_to_recorded: { actual_lateral_accel: { rmse: 0.01, mae: 0.005, bias: -0.002, p95_abs_error: 0.015 } },
        },
        quality: { max_ensemble_disagreement_p95_normalized: 0.61 },
        provenance: {
          model_hash: 'a'.repeat(64),
          model: { name: 'Causal Ioniq plant' },
          telemetry_ndjson_sha256: 'b'.repeat(64),
          input_alignment: 'timestamp_causal_recorded_history_asof',
          extractor: {
            extractor: 'comma-companion-rlog',
            extractor_version: '2.0.0',
            source_starpilot_commit: '19f8c767',
          },
          full_rlog: {
            timeline_version: 'timeline-v2',
            source_objects: [
              { log_type: 'rlog', sha256: 'c'.repeat(64) },
              { log_type: 'qlog', sha256: 'd'.repeat(64) },
            ],
          },
        },
      },
    })

    expect(result.exact_baseline).toBe(false)
    expect(result.warnings[0]).toMatchObject({
      code: 'model_disagreement',
      severity: 'blocker',
      details: { p95_normalized: 0.61 },
    })
    expect(result.warnings.at(-1)).toMatchObject({ severity: 'info', message: 'Read-only estimate.' })
    expect(result.uncertainty.candidate_std.map((point) => point.value)).toEqual([0.03, 0.04])
    expect(result.uncertainty.maximum_disagreement_p95_normalized).toBe(0.61)
    expect(result.metrics.find((metric) => metric.label === 'BIAS')?.objective).toBe('absolute_lower')
    expect(result.provenance).toMatchObject({
      model_name: 'Causal Ioniq plant',
      starpilot_commit: '19f8c767',
      extractor_version: 'comma-companion-rlog · 2.0.0',
      telemetry_hash: 'b'.repeat(64),
      timeline_version: 'timeline-v2',
      rlog_hashes: ['c'.repeat(64)],
    })
  })
})

describe('device normalization', () => {
  it('reads nested host metrics and preserves unknown heartbeat/offroad state', () => {
    const device = normalizeDevice({
      id: 'comma-1',
      display_name: 'Ioniq',
      enrolled_at: '2026-01-01T00:00:00Z',
      last_seen_at: null,
      online: true,
      offroad: null,
      agent_version: '1',
      software_version: null,
      network_type: 'wifi',
      state: 'idle',
      capabilities: ['command_rescan'],
      metrics: {
        pending_bytes: 12,
        host: {
          data_free_bytes: 345,
          current_route: 'route-1',
          starpilot: { branch: 'starpilot', commit: 'abc123' },
          temperatures: [{ name: 'cpu', celsius: 61.5 }],
          power_supplies: [{ metrics: { capacity: 78 } }],
        },
      },
    })

    expect(device.last_seen_at).toBeUndefined()
    expect(device.onroad).toBeUndefined()
    expect(device.offroad).toBeUndefined()
    expect(device.state).toBe('warning')
    expect(device).toMatchObject({
      git_branch: 'starpilot',
      git_commit: 'abc123',
      current_drive_id: 'route-1',
      queue_bytes: 12,
      free_space_bytes: 345,
      temperature_c: 61.5,
      battery_percent: 78,
    })
  })

  it('uses the complete agent queue instead of only the declared server upload', async () => {
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      if (url === '/api/v1/uploads/snapshot') {
        return new Response(JSON.stringify({
          active_uploads: 1,
          failed_uploads: 0,
          completed_uploads: 0,
          bytes_received: 100_000_000,
          bytes_expected: 155_000_000,
          pending_bytes: 55_000_000,
          bytes_per_second_60s: 1_875_000,
          by_device: {},
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      if (url === '/api/v1/devices') {
        return new Response(JSON.stringify([{
          id: 'comma-1',
          display_name: 'Ioniq',
          enrolled_at: '2026-01-01T00:00:00Z',
          last_seen_at: '2026-07-29T10:00:00Z',
          online: true,
          offroad: true,
          agent_version: '1',
          software_version: null,
          network_type: 'wifi',
          state: 'uploading',
          capabilities: [],
          metrics: { pending_bytes: 7_000_000_000 },
        }]), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      throw new Error(`Unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    try {
      await expect(api.inbound()).resolves.toMatchObject({
        devices_online: 1,
        devices_total: 1,
        upload_bps: 1_875_000,
        pending_upload_bytes: 7_000_000_000,
        server_pending_bytes: 55_000_000,
        bytes_received: 100_000_000,
      })
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('drive catalog normalization', () => {
  it('does not call a segment playback-ready when only raw/rlog artifacts are ready', () => {
    const drive = normalizeDriveDetail({
      id: 'drive-1',
      device_id: 'comma-1',
      route_name: 'route-1',
      started_at: '2026-01-01T00:00:00Z',
      ended_at: null,
      duration_us: 60_000_000,
      segment_count: 1,
      ready_segments: 1,
      expected_media: 1,
      ready_media: 0,
      missing_media: 1,
      failed_media: 0,
      pruned_media: 0,
      raw_video_pruning_required: true,
      backup_bytes_received: 100,
      backup_bytes_expected: 100,
      artifact_count: 2,
      expected_rlogs: 1,
      archived_rlogs: 1,
      rlog_backup_complete: true,
      telemetry_ready: true,
      telemetry_status: 'ready',
      readiness: 'ready',
      cameras: [{
        id: 'road',
        label: 'Road',
        available: false,
        codec: null,
        width: null,
        height: null,
        fps: null,
      }, {
        id: 'driver',
        label: 'Driver',
        available: false,
        codec: null,
        width: null,
        height: null,
        fps: null,
      }],
      vehicle: null,
      distance_m: null,
      location_start: null,
      location_end: null,
      raw_bytes: 120,
      derived_bytes: 0,
      stored_bytes: 120,
      poster_url: null,
      thumbnail_url: null,
      created_at: '2026-01-01T00:00:00Z',
      telemetry_generation: null,
      simulation_eligible: false,
      simulation_eligibility_reasons: [],
      segments: [{
        id: 'segment-0',
        number: 0,
        start_t_us: 0,
        duration_us: 60_000_000,
        expected_streams: [{
          role: 'road-camera',
          artifact_type: 'fcamera',
          camera: 'road',
          manifest_status: 'present',
          relative_path: '0/fcamera.hevc',
          archive_status: 'stored',
          media_status: 'pending',
        }, {
          role: 'driver-camera',
          artifact_type: 'dcamera',
          camera: 'driver',
          manifest_status: 'missing',
          relative_path: null,
          archive_status: 'missing',
          media_status: 'pending',
        }],
        artifacts: [
          {
            id: 'raw-video',
            kind: 'fcamera',
            camera: 'road',
            size: 100,
            mime_type: 'video/hevc',
            codec: 'hevc',
            duration_us: 60_000_000,
            status: 'ready',
          },
          {
            id: 'rlog',
            kind: 'rlog',
            camera: null,
            size: 20,
            mime_type: null,
            codec: null,
            duration_us: null,
            status: 'ready',
          },
        ],
      }],
      route_inventory: {
        manifest_sha256: 'a'.repeat(64),
        generation: 1,
        state: 'complete',
        route_closed: true,
        capability_source: 'configured+route_union',
        closure_evidence: ['route_closed'],
        expected_streams: [{
          role: 'road-camera',
          root_name: 'fcamera.hevc',
          artifact_type: 'fcamera',
          camera: 'road',
        }, {
          role: 'driver-camera',
          root_name: 'dcamera.hevc',
          artifact_type: 'dcamera',
          camera: 'driver',
        }],
        missing_segment_numbers: [],
        declared_file_count: 3,
        archived_file_count: 2,
        missing_file_count: 1,
      },
    })

    expect(drive.cameras).toEqual(expect.arrayContaining([
      expect.objectContaining({ id: 'road', available: false }),
      expect.objectContaining({ id: 'driver', available: false }),
    ]))
    expect(drive.segments[0]).toMatchObject({
      state: 'partial',
      camera_readiness: { road: 'processing', driver: 'partial' },
      missing: expect.arrayContaining([
        'Road AV1 processing',
        'Driver AV1 partial',
        'driver-camera: missing from device manifest',
      ]),
    })
    expect(drive.route_inventory).toMatchObject({
      generation: 1,
      expected_streams: expect.arrayContaining([
        expect.objectContaining({ role: 'road-camera', camera: 'road' }),
        expect.objectContaining({ role: 'driver-camera', camera: 'driver' }),
      ]),
    })
  })
})

describe('catalog pagination and dashboard totals', () => {
  const rawDrive = {
    id: 'drive-1',
    device_id: 'comma-1',
    route_name: 'route-1',
    started_at: '2026-01-01T00:00:00Z',
    ended_at: '2026-01-01T00:01:00Z',
    duration_us: 60_000_000,
    segment_count: 1,
    ready_segments: 1,
    expected_media: 1,
    ready_media: 1,
    missing_media: 0,
    failed_media: 0,
    pruned_media: 1,
    raw_video_pruning_required: true,
    backup_bytes_received: 100,
    backup_bytes_expected: 100,
    artifact_count: 3,
    expected_rlogs: 1,
    archived_rlogs: 1,
    rlog_backup_complete: true,
    telemetry_ready: true,
    telemetry_status: 'ready',
    readiness: 'ready',
    cameras: [{
      id: 'road',
      label: 'Road',
      available: true,
      codec: 'av1',
      width: 1928,
      height: 1208,
      fps: 20,
    }],
    vehicle: 'HYUNDAI_IONIQ_5',
    distance_m: 1_234,
    location_start: 'Oslo',
    location_end: 'Drammen',
    raw_bytes: 100,
    derived_bytes: 25,
    stored_bytes: 125,
    poster_url: '/api/v1/artifacts/poster/content',
    thumbnail_url: '/api/v1/artifacts/thumb/content',
    created_at: '2026-01-01T00:00:00Z',
  }

  const jsonResponse = (value: unknown) => new Response(JSON.stringify(value), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  })

  it('retains filtered summary totals separately from the current page', async () => {
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      items: [rawDrive],
      total: 81,
      limit: 25,
      offset: 50,
      summary: {
        duration_us: 9_000_000_000,
        stored_bytes: 123_456,
        by_readiness: {
          importing: 1,
          processing: 2,
          ready: 75,
          partial: 3,
          failed: 0,
        },
      },
    }))
    vi.stubGlobal('fetch', fetchMock)

    try {
      const page = await api.drives('ioniq', 'ready', 25, 50)
      expect(page).toMatchObject({
        total: 81,
        limit: 25,
        offset: 50,
        summary: {
          duration_us: 9_000_000_000,
          stored_bytes: 123_456,
          by_readiness: { ready: 75 },
        },
        items: [{
          id: 'drive-1',
          vehicle: 'HYUNDAI_IONIQ_5',
          stored_bytes: 125,
          poster_url: '/api/v1/artifacts/poster/content',
          cameras: [expect.objectContaining({ id: 'road', available: true, codec: 'av1' })],
        }],
      })
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/v1/drives?q=ioniq&readiness=ready&limit=25&offset=50',
        expect.anything(),
      )
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('uses global dashboard readiness and host-capacity values, not the recent page', async () => {
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      if (url === '/api/v1/dashboard') {
        return jsonResponse({
          generated_at: '2026-01-01T00:02:00Z',
          devices_total: 2,
          devices_online: 1,
          drives_total: 250,
          drives_ready: 201,
          drives_by_readiness: {
            importing: 4,
            processing: 30,
            ready: 201,
            partial: 12,
            failed: 3,
          },
          segments_total: 1_000,
          artifacts_total: 3_000,
          raw_bytes: 400,
          derived_bytes: 100,
          storage_cataloged_bytes: 500,
          storage_capacity_bytes: 10_000,
          storage_used_bytes: 6_000,
          storage_free_bytes: 4_000,
          upload: {
            active_uploads: 0,
            failed_uploads: 0,
            completed_uploads: 9,
            bytes_received: 0,
            bytes_expected: 0,
            pending_bytes: 0,
            bytes_per_second_60s: 0,
            by_device: {},
          },
          commands_by_state: {},
          jobs_by_state: { queued: 2, leased: 1, running: 3 },
          worker: {
            last_seen: '2026-01-01T00:01:00Z',
            stale: true,
            online: false,
            transcode_jobs_remaining: 9,
            transcode_seconds_per_job: 60,
            eta_seconds: 540,
          },
          archive: {
            available: true,
            writable: false,
          },
        })
      }
      if (
        url === '/api/v1/uploads?limit=8&offset=0&state=receiving' ||
        url === '/api/v1/uploads?limit=8&offset=0&state=finalizing'
      ) {
        return jsonResponse({ items: [], total: 0 })
      }
      if (url === '/api/v1/drives?limit=6&offset=0') {
        return jsonResponse({
          items: [rawDrive],
          total: 250,
          limit: 6,
          offset: 0,
          summary: {
            duration_us: 60_000_000,
            stored_bytes: 125,
            by_readiness: { ready: 1 },
          },
        })
      }
      throw new Error(`Unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    try {
      const overview = await api.overview()
      expect(overview).toMatchObject({
        drives_total: 250,
        drives_ready: 201,
        drives_by_readiness: { processing: 30, ready: 201 },
        jobs_active: 6,
        worker_transcode_jobs_remaining: 9,
        worker_eta_seconds: 540,
        storage: {
          used_bytes: 6_000,
          capacity_bytes: 10_000,
          free_bytes: 4_000,
          cataloged_bytes: 500,
          raw_bytes: 400,
          derived_bytes: 100,
        },
        recent_drives: [{ id: 'drive-1' }],
      })
      expect(overview.services.find((service) => service.id === 'worker')).toMatchObject({
        state: 'error',
        detail: 'heartbeat stale · 9 encodes remaining · ETA 9 min',
        updated_at: '2026-01-01T00:01:00Z',
      })
      expect(overview.services.find((service) => service.id === 'archive')).toMatchObject({
        state: 'error',
        detail: 'archive root is not writable',
      })
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('upload finalization state', () => {
  it('loads receiving and finalizing rows for the active pipeline', async () => {
    const response = (body: unknown) => new Response(JSON.stringify(body), {
      status: 200,
      headers: { 'content-type': 'application/json' },
    })
    const rawUpload = {
      device_id: 'comma-1',
      route_name: 'route-1',
      segment_number: 0,
      artifact_type: 'video',
      camera: 'road',
      offset: 50,
      length: 100,
      durable: false,
      error: null,
      created_at: '2026-01-01T00:00:00Z',
      bytes_per_second: 10,
    }
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      if (url.endsWith('state=receiving')) {
        return response({
          items: [{
            ...rawUpload,
            id: 'uploading-1',
            upload_id: 'uploading-1',
            relative_path: 'route/0/ecamera.hevc',
            status: 'receiving',
            updated_at: '2026-01-01T00:00:01Z',
          }],
          total: 1,
        })
      }
      if (url.endsWith('state=finalizing')) {
        return response({
          items: [{
            ...rawUpload,
            id: 'verifying-1',
            upload_id: 'verifying-1',
            relative_path: 'route/0/dcamera.hevc',
            status: 'finalizing',
            updated_at: '2026-01-01T00:00:02Z',
          }],
          total: 1,
        })
      }
      throw new Error(`Unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    try {
      await expect(api.activeUploads()).resolves.toMatchObject([
        { id: 'verifying-1', state: 'verifying' },
        { id: 'uploading-1', state: 'uploading' },
      ])
      expect(fetchMock).toHaveBeenCalledTimes(2)
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('maps the durable-finalization phase to the verifying UI state', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      items: [{
        id: 'upload-1',
        upload_id: 'upload-1',
        device_id: 'comma-1',
        relative_path: 'route/0/fcamera.hevc',
        route_name: 'route-1',
        segment_number: 0,
        artifact_type: 'video',
        camera: 'road',
        offset: 100,
        length: 100,
        status: 'finalizing',
        durable: false,
        error: null,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:01Z',
        bytes_per_second: 0,
      }],
      total: 1,
    }), { status: 200, headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)

    try {
      await expect(api.uploadPage('verifying', 25, 50, 'route 1')).resolves.toMatchObject({
        items: [
          expect.objectContaining({ id: 'upload-1', state: 'verifying', durable: false }),
        ],
        total: 1,
        limit: 25,
        offset: 50,
      })
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/v1/uploads?q=route+1&limit=25&offset=50&state=finalizing',
        expect.anything(),
      )
    } finally {
      vi.unstubAllGlobals()
    }
  })

  it('passes job search, state, and pagination to the server-filtered list', async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      items: [],
      total: 47,
    }), { status: 200, headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)

    try {
      await expect(api.jobs({
        state: 'failed',
        q: 'checksum mismatch',
        limit: 25,
        offset: 50,
      })).resolves.toEqual({ items: [], total: 47 })
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/v1/jobs?state=failed&q=checksum+mismatch&limit=25&offset=50',
        expect.anything(),
      )
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('generation-pinned telemetry normalization', () => {
  const rawSeries = {
    drive_id: 'drive-1',
    ndjson_sha256: 'a'.repeat(64),
    timeline_version: 'b'.repeat(64),
    timeline_origin: 'stable' as const,
    start_t_us: 0,
    end_t_us: 100,
    signals: [],
    markers: [],
    markers_truncated: false,
  }

  it('retains generation provenance and rejects a stale response', () => {
    expect(normalizeSeries(rawSeries)).toMatchObject({
      drive_id: 'drive-1',
      ndjson_sha256: 'a'.repeat(64),
      timeline_version: 'b'.repeat(64),
      timeline_origin: 'stable',
    })
    expect(() => normalizeSeries(rawSeries, {
      drive_id: 'drive-1',
      telemetry_sha256: 'c'.repeat(64),
      timeline_version: 'b'.repeat(64),
    })).toThrow(ApiError)
  })

  it('maps marker kinds into timeline lanes and preserves explicit gap intervals', () => {
    const series = normalizeSeries({
      ...rawSeries,
      start_t_us: 500_000,
      end_t_us: 2_000_000,
      markers_truncated: true,
      markers: [
        {
          id: 'telemetry-gap',
          kind: 'telemetry_gap',
          start_t_us: 1_000_000,
          end_t_us: 1_000_000,
          severity: 'warning',
          label: 'Rlog gap',
          attributes: { gap_us: 100_000 },
        },
        {
          id: 'camera-gap',
          kind: 'camera_gap',
          start_t_us: 1_100_000,
          end_t_us: 1_200_000,
          severity: null,
          label: null,
          attributes: {},
        },
        {
          id: 'overlay',
          kind: 'driver_overlay',
          start_t_us: 1_300_000,
          end_t_us: 1_300_000,
          severity: 'warning',
          label: 'Driver torque',
          attributes: {},
        },
        {
          id: 'active',
          kind: 'controls_active',
          start_t_us: 1_400_000,
          end_t_us: 1_500_000,
          severity: null,
          label: null,
          attributes: {},
        },
        {
          id: 'segment-event',
          kind: 'segment_boundary',
          start_t_us: 1_600_000,
          end_t_us: 1_600_000,
          severity: 'critical',
          label: 'Segment 2',
          attributes: {},
        },
      ],
    })

    expect(series.gaps).toEqual([
      {
        start_us: 900_000,
        end_us: 1_000_000,
        reason: 'Rlog gap',
        kind: 'telemetry_gap',
        severity: 'warning',
      },
      {
        start_us: 1_100_000,
        end_us: 1_200_000,
        reason: 'camera gap',
        kind: 'camera_gap',
        severity: 'info',
      },
    ])
    expect(series.events).toEqual([
      expect.objectContaining({ id: 'overlay', lane: 'driver_overlay', severity: 'warning' }),
      expect.objectContaining({ id: 'active', lane: 'active', severity: 'info' }),
      expect.objectContaining({ id: 'segment-event', lane: 'alert', severity: 'critical' }),
    ])
    expect(series.markers_truncated).toBe(true)
  })
})

describe('simulation job lifecycle', () => {
  it('requests cooperative cancellation and polls the simulation to a terminal state', async () => {
    const controller = new AbortController()
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      const url = String(input)
      if (url.endsWith('/api/v1/drives/drive-1/simulations')) {
        return new Response(JSON.stringify({
          id: 'simulation-1',
          job_id: 'job-1',
          state: 'queued',
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      if (url.endsWith('/api/v1/jobs/job-1/cancel')) {
        return new Response(JSON.stringify({}), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.endsWith('/api/v1/simulations/simulation-1')) {
        return new Response(JSON.stringify({
          id: 'simulation-1',
          drive_id: 'drive-1',
          job_id: 'job-1',
          t_us: 4_000_000,
          horizon_us: 2_000_000,
          model_hash: 'a'.repeat(64),
          mode: 'approximate_closed_loop',
          parameters: { damping_gain: 0.02 },
          telemetry_sha256: 'b'.repeat(64),
          timeline_version: 'c'.repeat(64),
          state: 'canceled',
          progress: 0,
          result: {},
          error: 'Canceled by operator',
        }), { status: 200, headers: { 'content-type': 'application/json' } })
      }
      throw new Error(`Unexpected request ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)

    try {
      await expect(api.simulate({
        drive_id: 'drive-1',
        model_id: 'a'.repeat(64),
        start_us: 4_000_000,
        horizon_us: 2_000_000,
        telemetry_sha256: 'b'.repeat(64),
        timeline_version: 'c'.repeat(64),
        parameters: { damping_gain: 0.02 },
      }, {
        signal: controller.signal,
        onProgress: (progress) => {
          if (progress.state === 'queued') controller.abort()
        },
      })).rejects.toMatchObject({ code: 'simulation_canceled' })
      expect(fetchMock.mock.calls.map(([input]) => String(input))).toContain(
        '/api/v1/jobs/job-1/cancel',
      )
    } finally {
      vi.unstubAllGlobals()
    }
  })
})

describe('model capability normalization', () => {
  it('uses nested registry metadata and keeps disabled legacy models ineligible', () => {
    const model = normalizeModel({
      sha256: 'a'.repeat(64),
      name: 'Legacy plant',
      mode: 'approximate_closed_loop',
      enabled: false,
      eligible: false,
      metadata: {
        blocked_reason: 'blocked_pending_causal_retrain',
        causal_training_eligible: false,
        model: {
          artifact: 'plant.pt',
          model_type: 'neural_lateral_plant',
          member_count: 3,
        },
        adapter: {
          target_car_fingerprint: 'HYUNDAI_IONIQ_5',
          history_s: 3,
          max_horizon_s: 2,
        },
      },
    }, {
      available: false,
      modes: ['approximate_closed_loop'],
      models: [],
      history_required_us: 1,
      default_horizon_us: 1,
      maximum_horizon_us: 1,
      parameter_schema: {},
    })

    expect(model).toMatchObject({
      version: 'plant.pt',
      vehicle: 'HYUNDAI_IONIQ_5',
      history_us: 3_000_000,
      horizon_us: 2_000_000,
      enabled: false,
      eligible: false,
      state: 'idle',
    })
    expect(model.eligibility_reasons).toContain('model is disabled')
  })
})

describe('parameter schema normalization', () => {
  it('retains runtime support and model-only scope', () => {
    const schema = normalizeParameterSchema('model-1', [
      {
        name: 'damping_gain',
        label: 'Damping',
        category: 'damping',
        type: 'number',
        default: 0.02,
        scope: 'runtime',
        runtime_supported: true,
      },
      {
        name: 'turn_exit_damping_gain',
        label: 'Turn-exit damping',
        category: 'damping',
        type: 'number',
        default: 0.01,
        scope: 'model_only',
        runtime_supported: false,
      },
    ])

    expect(schema.parameters[0]).toMatchObject({ scope: 'runtime', runtime_supported: true })
    expect(schema.parameters[1]).toMatchObject({ scope: 'model_only', runtime_supported: false })
  })

  it('retains units and advanced grouping from the public JSON Schema extensions', () => {
    const schema = normalizeParameterSchema('model-1', {
      type: 'object',
      properties: {
        friction: {
          type: 'number',
          title: 'Friction',
          default: 0.1,
          'x-units': 'Nm',
          'x-advanced': true,
          'x-scope': 'runtime',
          'x-runtime-supported': true,
        },
      },
    })

    expect(schema.parameters[0]).toMatchObject({
      unit: 'Nm',
      group: 'Advanced',
      scope: 'runtime',
      runtime_supported: true,
    })
  })
})
