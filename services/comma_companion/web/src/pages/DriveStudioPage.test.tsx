import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { MediaManifest, MediaSyncIndex, ModelSummary } from '../api/types'

const apiMocks = vi.hoisted(() => ({
  mediaManifest: vi.fn(),
  mediaSync: vi.fn(),
  parameterSchema: vi.fn(),
  simulate: vi.fn(),
}))

vi.mock('../api/client', () => ({
  api: apiMocks,
  demoMode: false,
}))

import { DriveMedia, TuneWorkbench, valueAt } from './DriveStudioPage'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function manifest(syncMode: 'exact' | 'approximate'): MediaManifest {
  return {
    drive_id: 'drive-1',
    camera: 'road',
    synchronized: syncMode === 'exact',
    items: [{
      segment_number: 0,
      start_t_us: 1_000_000,
      duration_us: 1_000_000,
      artifact_id: 'video-0',
      url: '/api/v1/artifacts/video-0/media',
      fps: 20,
      sync_mode: syncMode,
      sync_url: syncMode === 'exact'
        ? '/api/v1/drives/drive-1/media-sync?camera=road&segment=0&telemetry_sha256=t&frame_index_sha256=f&frame_index_artifact_id=frames-0&video_sha256=v&video_artifact_id=video-0'
        : null,
      frame_index_artifact_id: syncMode === 'exact' ? 'frames-0' : null,
      frame_index_url: syncMode === 'exact' ? '/api/v1/artifacts/frames-0/media' : null,
      sync_reason: syncMode === 'approximate' ? 'No rlog frame join is available.' : null,
      video_sha256: syncMode === 'exact' ? 'v' : null,
      timeline_origin: syncMode === 'exact' ? 'stable' : null,
    }],
  }
}

function syncIndex(): MediaSyncIndex {
  return {
    drive_id: 'drive-1',
    camera: 'road',
    segment_number: 0,
    video_artifact_id: 'video-0',
    video_sha256: 'v',
    frame_index_artifact_id: 'frames-0',
    frame_index_sha256: 'f',
    telemetry_sha256: 't',
    timeline_version: 'frame-telemetry-v1',
    timeline_origin: 'stable',
    mode: 'exact',
    coverage: {
      start_t_us: 1_000_000,
      end_t_us: 1_100_000,
      first_pts_us: 100_000,
      last_end_pts_us: 250_000,
    },
    points: [
      { segment_frame_id: 0, pts_us: 100_000, duration_us: 50_000, drive_t_us: 1_000_000, keyframe: true },
      { segment_frame_id: 1, pts_us: 150_000, duration_us: 50_000, drive_t_us: 1_050_000, keyframe: false },
      { segment_frame_id: 2, pts_us: 200_000, duration_us: 50_000, drive_t_us: 1_100_000, keyframe: false },
    ],
  }
}

describe('DriveMedia synchronization state', () => {
  it('labels linear segment-time fallback as approximate', async () => {
    apiMocks.mediaManifest.mockResolvedValueOnce(manifest('approximate'))
    const onStatus = vi.fn()

    render(
      <DriveMedia
        driveId="drive-1"
        camera="road"
        durationUs={2_000_000}
        playheadUs={1_000_000}
        onPlayhead={vi.fn()}
        onSyncStatus={onStatus}
        telemetrySha256="t"
        timelineVersion="frame-telemetry-v1"
      />,
    )

    expect(await screen.findByText('APPROXIMATE ALIGNMENT')).toBeInTheDocument()
    expect(apiMocks.mediaSync).not.toHaveBeenCalled()
    await waitFor(() => {
      expect(onStatus).toHaveBeenCalledWith(expect.objectContaining({
        mode: 'approximate',
        reason: 'No rlog frame join is available.',
      }))
    })
  })

  it('loads a pinned exact timeline and frame-steps using adjacent mappings', async () => {
    apiMocks.mediaManifest.mockResolvedValueOnce(manifest('exact'))
    apiMocks.mediaSync.mockResolvedValueOnce(syncIndex())
    const onPlayhead = vi.fn()

    render(
      <DriveMedia
        driveId="drive-1"
        camera="road"
        durationUs={2_000_000}
        playheadUs={1_000_000}
        onPlayhead={onPlayhead}
        telemetrySha256="t"
        timelineVersion="frame-telemetry-v1"
      />,
    )

    expect(await screen.findByText('EXACT FRAME SYNC')).toBeInTheDocument()
    expect(apiMocks.mediaSync).toHaveBeenCalledWith(
      '/api/v1/drives/drive-1/media-sync?camera=road&segment=0&telemetry_sha256=t&frame_index_sha256=f&frame_index_artifact_id=frames-0&video_sha256=v&video_artifact_id=video-0',
    )

    fireEvent.click(screen.getByRole('button', { name: 'Forward one frame' }))
    await waitFor(() => expect(onPlayhead).toHaveBeenCalledWith(1_050_000, false))
  })

  it('forces a post-transition manifest read when processing becomes ready', async () => {
    let resolveInitial!: (value: MediaManifest) => void
    const initial = new Promise<MediaManifest>((resolve) => {
      resolveInitial = resolve
    })
    apiMocks.mediaManifest
      .mockReturnValueOnce(initial)
      .mockResolvedValue(manifest('approximate'))

    const view = render(
      <DriveMedia
        driveId="drive-1"
        camera="road"
        durationUs={2_000_000}
        playheadUs={1_000_000}
        onPlayhead={vi.fn()}
        telemetrySha256="t"
        timelineVersion="frame-telemetry-v1"
        refreshWhileProcessing
      />,
    )
    await waitFor(() => expect(apiMocks.mediaManifest).toHaveBeenCalledTimes(1))

    view.rerender(
      <DriveMedia
        driveId="drive-1"
        camera="road"
        durationUs={2_000_000}
        playheadUs={1_000_000}
        onPlayhead={vi.fn()}
        telemetrySha256="t"
        timelineVersion="frame-telemetry-v1"
        refreshWhileProcessing={false}
      />,
    )
    resolveInitial({ drive_id: 'drive-1', camera: 'road', synchronized: false, items: [] })

    await waitFor(() => expect(apiMocks.mediaManifest).toHaveBeenCalledTimes(2))
  })
})

describe('Tune model eligibility', () => {
  it('keeps a disabled legacy model historical-only and never requests its parameter schema', async () => {
    const blockedModel: ModelSummary = {
      id: 'a'.repeat(64),
      name: 'Legacy plant',
      version: 'legacy.pt',
      state: 'idle',
      vehicle: 'HYUNDAI_IONIQ_5',
      horizon_us: 2_000_000,
      history_us: 3_000_000,
      hash: 'a'.repeat(64),
      mode: 'approximate_closed_loop',
      enabled: false,
      eligible: false,
      eligibility_reasons: ['blocked pending causal retrain'],
    }

    render(
      <TuneWorkbench
        driveId="drive-1"
        playheadUs={4_000_000}
        durationUs={10_000_000}
        models={[blockedModel]}
        simulationEligible
        simulationEligibilityReasons={[]}
        telemetrySha256={'b'.repeat(64)}
        timelineVersion={'c'.repeat(64)}
      />,
    )

    expect(screen.getByText('No model is eligible for counterfactual replay')).toBeInTheDocument()
    await waitFor(() => expect(apiMocks.parameterSchema).not.toHaveBeenCalled())
  })
})

describe('playhead telemetry inspection', () => {
  const signal = {
    id: 'vehicle.speed',
    label: 'Speed',
    unit: 'm/s',
    points: [
      { t_us: 900_000, value: 10 },
      { t_us: 1_250_000, value: 12 },
    ],
  }

  it('shows only a sample within the maximum age', () => {
    expect(valueAt(signal, 1_000_000)).toBe(10)
    expect(valueAt(signal, 1_100_001)).toBeNull()
  })

  it('does not bridge a telemetry gap but ignores an unrelated camera gap', () => {
    expect(valueAt(signal, 1_000_000, [{
      start_us: 950_000,
      end_us: 975_000,
      reason: 'Rlog gap',
      kind: 'telemetry_gap',
    }])).toBeNull()
    expect(valueAt(signal, 1_000_000, [{
      start_us: 950_000,
      end_us: 975_000,
      reason: 'Road camera gap',
      kind: 'camera_gap',
    }])).toBe(10)
  })
})
