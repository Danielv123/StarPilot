import { describe, expect, it } from 'vitest'
import type { InboundStatus } from '../api/types'
import {
  ACTIVE_PIPELINE_POLL_INTERVAL_MS,
  backlogDetail,
  deviceRoadStateDetail,
  instantaneousSpeed,
  sparklinePath,
} from './OverviewPage'

function inbound(overrides: Partial<InboundStatus> = {}): InboundStatus {
  return {
    generated_at: '2026-07-29T10:00:00.000Z',
    devices_online: 1,
    devices_total: 1,
    devices_onroad: 1,
    devices_parked: 0,
    devices_road_state_unknown: 0,
    upload_bps: 2_000_000,
    pending_upload_bytes: 7_000_000_000,
    unuploaded_bytes: 14_000_000_000,
    unuploaded_files: 219,
    protected_spool_bytes: 12_900_000_000,
    backlog_scope: 'full',
    backlog_scan_complete: true,
    device_metrics_at: '2026-07-29T10:00:00.000Z',
    device_metrics_stale: false,
    server_pending_bytes: 55_000_000,
    bytes_received: 100_000_000,
    ...overrides,
  }
}

describe('overview inbound telemetry', () => {
  it('refreshes active upload rows once per second', () => {
    expect(ACTIVE_PIPELINE_POLL_INTERVAL_MS).toBe(1_000)
  })

  it('shows live onroad and parked counts instead of a hard-coded parked label', () => {
    expect(deviceRoadStateDetail(inbound(), 1)).toBe('1 enrolled · 1 onroad')
    expect(deviceRoadStateDetail(inbound({
      devices_online: 3,
      devices_total: 4,
      devices_onroad: 1,
      devices_parked: 1,
      devices_road_state_unknown: 1,
    }), 4)).toBe('4 enrolled · 1 onroad · 1 parked · 1 state unknown')
  })

  it('derives a one-second bandwidth sample from received-byte deltas', () => {
    expect(instantaneousSpeed(
      inbound(),
      inbound({
        generated_at: '2026-07-29T10:00:01.000Z',
        bytes_received: 103_000_000,
      }),
    )).toBe(3_000_000)
  })

  it('falls back to the server rate after a counter reset', () => {
    expect(instantaneousSpeed(
      inbound(),
      inbound({
        generated_at: '2026-07-29T10:00:01.000Z',
        bytes_received: 1,
      }),
    )).toBe(2_000_000)
  })

  it('builds a stable full-width sparkline path', () => {
    expect(sparklinePath([0, 5, 10])).toBe('M0.00,34.00 L50.00,19.00 L100.00,4.00')
  })

  it('separates the complete backlog from protected and server-active bytes', () => {
    expect(backlogDetail(inbound())).toContain('219 files')
    expect(backlogDetail(inbound())).toContain('12.0 GB protected')
    expect(backlogDetail(inbound())).toContain('52.5 MB server-active')
  })

  it('labels legacy agent telemetry as protected-spool-only', () => {
    expect(backlogDetail(inbound({ backlog_scope: 'protected' }))).toContain(
      'protected spool only; agent update pending',
    )
  })
})
