import { describe, expect, it } from 'vitest'
import type { InboundStatus } from '../api/types'
import { instantaneousSpeed, sparklinePath } from './OverviewPage'

function inbound(overrides: Partial<InboundStatus> = {}): InboundStatus {
  return {
    generated_at: '2026-07-29T10:00:00.000Z',
    devices_online: 1,
    devices_total: 1,
    upload_bps: 2_000_000,
    pending_upload_bytes: 7_000_000_000,
    server_pending_bytes: 55_000_000,
    bytes_received: 100_000_000,
    ...overrides,
  }
}

describe('overview inbound telemetry', () => {
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
})
