import { describe, expect, it } from 'vitest'
import type { Drive } from '../api/types'
import { driveProgressSummary, driveStatusLabel } from './RecordRows'

function drive(overrides: Partial<Drive> = {}): Drive {
  return {
    id: 'drive-1',
    device_id: 'comma-1',
    route_name: 'route-1',
    started_at: '2026-07-29T10:00:00Z',
    duration_us: 60_000_000,
    readiness: 'processing',
    segment_count: 2,
    ready_segments: 2,
    expected_media: 2,
    ready_media: 2,
    missing_media: 0,
    failed_media: 0,
    pruned_media: 1,
    raw_video_pruning_required: true,
    backup_bytes_received: 75,
    backup_bytes_expected: 100,
    artifact_count: 5,
    cameras: [],
    telemetry_ready: false,
    ...overrides,
  }
}

describe('drive catalog progress', () => {
  it('separates uploaded bytes from AV1 and pruning work', () => {
    expect(driveProgressSummary(drive())).toEqual({
      backupPercent: 75,
      backupDetail: '75 B / 100 B',
      processingPercent: 75,
      processingDetail: '2/2 AV1 · 1/2 pruned',
      processingComplete: false,
    })
  })

  it('identifies media-complete routes that are only waiting for telemetry', () => {
    const completedMedia = drive({ pruned_media: 2 })
    expect(driveProgressSummary(completedMedia).processingPercent).toBe(100)
    expect(driveStatusLabel(completedMedia)).toBe('awaiting telemetry')
  })

  it('does not require pruning when raw-video retention is enabled', () => {
    const retained = drive({
      pruned_media: 0,
      raw_video_pruning_required: false,
      telemetry_ready: true,
    })
    expect(driveProgressSummary(retained).processingComplete).toBe(true)
    expect(driveStatusLabel(retained)).toBe('finalizing')
  })

  it('never counts more pruned streams than ready AV1 streams', () => {
    const historicalDuplicates = drive({
      expected_media: 2,
      ready_media: 1,
      pruned_media: 20,
    })
    expect(driveProgressSummary(historicalDuplicates)).toMatchObject({
      processingPercent: 50,
      processingDetail: '1/2 AV1 · 1/2 pruned',
      processingComplete: false,
    })
  })
})
