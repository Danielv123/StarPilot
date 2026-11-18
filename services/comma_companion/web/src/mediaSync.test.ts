import { describe, expect, it } from 'vitest'
import type { MediaManifest, MediaManifestItem, MediaSyncIndex } from './api/types'
import {
  adjacentFrame,
  frameAtVideoPts,
  frameNearestDriveTime,
  mediaManifestEqual,
  mediaItemAtDriveTime,
  playbackTimeForAvailableMedia,
  validateMediaSyncIndex,
} from './mediaSync'

function exactIndex(): MediaSyncIndex {
  return {
    drive_id: 'drive-1',
    camera: 'road',
    segment_number: 7,
    video_artifact_id: 'video-7',
    video_sha256: 'video-sha',
    frame_index_artifact_id: 'frames-7',
    frame_index_sha256: 'frame-index-sha',
    telemetry_sha256: 'telemetry-sha',
    timeline_version: 'frame-telemetry-v1',
    timeline_origin: 'stable',
    mode: 'exact',
    coverage: {
      start_t_us: 12_000_000,
      end_t_us: 12_151_000,
      first_pts_us: 125_000,
      last_end_pts_us: 275_000,
    },
    points: [
      { segment_frame_id: 20, pts_us: 125_000, duration_us: 50_000, drive_t_us: 12_001_000, keyframe: true },
      { segment_frame_id: 21, pts_us: 175_000, duration_us: 50_000, drive_t_us: 12_051_000, keyframe: false },
      { segment_frame_id: 22, pts_us: 225_000, duration_us: 50_000, drive_t_us: 12_101_000, keyframe: false },
    ],
  }
}

function manifestItem(
  segment: number,
  startUs: number,
  durationUs: number,
): MediaManifestItem {
  return {
    segment_number: segment,
    start_t_us: startUs,
    duration_us: durationUs,
    artifact_id: `video-${segment}`,
    url: `/api/v1/artifacts/video-${segment}/media`,
    sync_mode: 'approximate',
    sync_url: null,
    frame_index_artifact_id: null,
    frame_index_url: null,
    sync_reason: 'no verified frame join',
    video_sha256: null,
    timeline_origin: null,
  }
}

const exactExpectation = {
  driveId: 'drive-1',
  camera: 'road',
  segmentNumber: 7,
  videoArtifactId: 'video-7',
  videoSha256: 'video-sha',
  frameIndexArtifactId: 'frames-7',
  frameIndexSha256: 'frame-index-sha',
  telemetrySha256: 'telemetry-sha',
  timelineVersion: 'frame-telemetry-v1',
  timelineOrigin: 'stable' as const,
}

describe('exact media timeline validation', () => {
  it('accepts a non-zero WebM first PTS when coverage and provenance match', () => {
    expect(validateMediaSyncIndex(exactIndex(), exactExpectation)).toBeUndefined()
  })

  it('rejects mismatched artifacts and non-contiguous frame IDs', () => {
    expect(validateMediaSyncIndex(exactIndex(), {
      ...exactExpectation,
      videoArtifactId: 'another-video',
    })).toMatch(/does not match/)

    const broken = exactIndex()
    broken.points[1] = { ...broken.points[1], segment_frame_id: 23 }
    expect(validateMediaSyncIndex(broken, exactExpectation)).toMatch(/not contiguous/)

    expect(validateMediaSyncIndex(exactIndex(), {
      ...exactExpectation,
      videoSha256: 'new-video-generation',
    })).toMatch(/video hash is stale/)
  })
})

describe('video and drive time mapping', () => {
  it('maps HTML media PTS to the exact presented source frame', () => {
    const index = exactIndex()
    expect(frameAtVideoPts(index, 124_999)).toBeUndefined()
    expect(frameAtVideoPts(index, 125_000)?.segment_frame_id).toBe(20)
    expect(frameAtVideoPts(index, 224_999)?.segment_frame_id).toBe(21)
    expect(frameAtVideoPts(index, 275_000)?.segment_frame_id).toBe(22)
    expect(frameAtVideoPts(index, 275_001)).toBeUndefined()
  })

  it('snaps a drive seek to the nearest exact frame and steps by frame ID', () => {
    const index = exactIndex()
    expect(frameNearestDriveTime(index, 12_075_000)?.segment_frame_id).toBe(21)
    expect(frameNearestDriveTime(index, 12_077_000)?.segment_frame_id).toBe(22)
    expect(frameNearestDriveTime(index, 11_999_999)).toBeUndefined()
    expect(adjacentFrame(index, 12_051_000, -1)?.drive_t_us).toBe(12_001_000)
    expect(adjacentFrame(index, 12_051_000, 1)?.drive_t_us).toBe(12_101_000)
    expect(adjacentFrame(index, 12_101_000, 1)).toBeUndefined()
  })
})

describe('segment selection', () => {
  it('selects by drive coverage without bridging archived gaps', () => {
    const items = [
      manifestItem(2, 120_000_000, 60_000_000),
      manifestItem(0, 0, 60_000_000),
    ]
    expect(mediaItemAtDriveTime(items, 59_999_999)?.item.segment_number).toBe(0)
    expect(mediaItemAtDriveTime(items, 90_000_000)).toBeUndefined()
    expect(mediaItemAtDriveTime(items, 120_000_000)?.item.segment_number).toBe(2)
    expect(mediaItemAtDriveTime(items, 180_000_000)?.item.segment_number).toBe(2)
  })

  it('snaps route time zero to the first actual camera coverage', () => {
    const items = [
      manifestItem(1, 60_014_709, 60_000_000),
      manifestItem(0, 14_709, 60_000_000),
    ]
    expect(playbackTimeForAvailableMedia(items, 0)).toBe(14_709)
    expect(playbackTimeForAvailableMedia(items, 20_000)).toBe(20_000)
  })
})

describe('manifest change detection', () => {
  const base: MediaManifest = {
    drive_id: 'drive-1',
    camera: 'road',
    synchronized: false,
    items: [manifestItem(0, 14_709, 60_000_000)],
  }

  it('treats independently decoded manifests with identical media as unchanged', () => {
    expect(mediaManifestEqual(base, JSON.parse(JSON.stringify(base)))).toBe(true)
  })

  it('detects newly playable media and synchronization changes', () => {
    expect(mediaManifestEqual(base, {
      ...base,
      items: [...base.items, manifestItem(1, 60_014_709, 60_000_000)],
    })).toBe(false)
    expect(mediaManifestEqual(base, { ...base, synchronized: true })).toBe(false)
  })
})
