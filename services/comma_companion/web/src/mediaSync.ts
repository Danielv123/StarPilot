import type {
  MediaManifest,
  MediaManifestItem,
  MediaSyncIndex,
  MediaSyncPoint,
} from './api/types'

export interface MediaSyncExpectation {
  driveId: string
  camera: string
  segmentNumber: number
  videoArtifactId: string
  videoSha256: string
  frameIndexArtifactId: string
  frameIndexSha256: string
  telemetrySha256: string
  timelineVersion: string
  timelineOrigin: 'stable' | 'provisional'
}

export interface MediaSyncUrlPins {
  videoArtifactId: string
  videoSha256: string
  frameIndexArtifactId: string
  frameIndexSha256: string
  telemetrySha256: string
}

export function mediaSyncUrlPins(url: string): MediaSyncUrlPins | undefined {
  let parsed: URL
  try {
    parsed = new URL(url, window.location.origin)
  } catch {
    return undefined
  }
  const value = (key: string) => parsed.searchParams.get(key) ?? ''
  const pins = {
    videoArtifactId: value('video_artifact_id'),
    videoSha256: value('video_sha256'),
    frameIndexArtifactId: value('frame_index_artifact_id'),
    frameIndexSha256: value('frame_index_sha256'),
    telemetrySha256: value('telemetry_sha256'),
  }
  return Object.values(pins).every(Boolean) ? pins : undefined
}

function finiteInteger(value: number): boolean {
  return Number.isSafeInteger(value)
}

export function validateMediaSyncIndex(
  index: MediaSyncIndex,
  expected: MediaSyncExpectation,
): string | undefined {
  if (index.mode !== 'exact') return 'The server did not return an exact frame timeline.'
  if (index.drive_id !== expected.driveId) return 'The frame timeline belongs to a different drive.'
  if (index.camera !== expected.camera) return 'The frame timeline belongs to a different camera.'
  if (index.segment_number !== expected.segmentNumber) return 'The frame timeline belongs to a different segment.'
  if (index.video_artifact_id !== expected.videoArtifactId) return 'The frame timeline does not match this video artifact.'
  if (index.video_sha256 !== expected.videoSha256) return 'The frame timeline video hash is stale.'
  if (index.frame_index_artifact_id !== expected.frameIndexArtifactId) return 'The frame timeline index artifact is stale.'
  if (index.frame_index_sha256 !== expected.frameIndexSha256) return 'The frame timeline index hash is stale.'
  if (index.telemetry_sha256 !== expected.telemetrySha256) return 'The frame timeline telemetry hash is stale.'
  if (index.timeline_version !== expected.timelineVersion) return 'The frame timeline generation is stale.'
  if (index.timeline_origin !== expected.timelineOrigin) return 'The frame timeline origin does not match the manifest.'
  if (
    !index.timeline_version ||
    !index.frame_index_sha256 ||
    !index.frame_index_artifact_id ||
    !index.telemetry_sha256 ||
    !index.video_sha256
  ) {
    return 'The frame timeline is missing immutable provenance.'
  }
  if (!index.points.length) return 'The frame timeline contains no frames.'

  const coverage = index.coverage
  if (
    !finiteInteger(coverage.start_t_us) ||
    !finiteInteger(coverage.end_t_us) ||
    !finiteInteger(coverage.first_pts_us) ||
    !finiteInteger(coverage.last_end_pts_us) ||
    coverage.end_t_us < coverage.start_t_us ||
    coverage.last_end_pts_us <= coverage.first_pts_us
  ) {
    return 'The frame timeline coverage is invalid.'
  }

  for (let pointIndex = 0; pointIndex < index.points.length; pointIndex += 1) {
    const point = index.points[pointIndex]
    if (
      !finiteInteger(point.segment_frame_id) ||
      !finiteInteger(point.pts_us) ||
      !finiteInteger(point.duration_us) ||
      !finiteInteger(point.drive_t_us) ||
      point.duration_us <= 0
    ) {
      return `Frame ${pointIndex} has invalid timestamps.`
    }
    if (pointIndex === 0) continue
    const previous = index.points[pointIndex - 1]
    if (point.segment_frame_id !== previous.segment_frame_id + 1) {
      return `Frame IDs are not contiguous at frame ${pointIndex}.`
    }
    if (point.pts_us <= previous.pts_us) {
      return `Video timestamps are not strictly increasing at frame ${pointIndex}.`
    }
    if (point.drive_t_us <= previous.drive_t_us) {
      return `Drive timestamps are not strictly increasing at frame ${pointIndex}.`
    }
  }

  const first = index.points[0]
  const last = index.points[index.points.length - 1]
  if (
    coverage.first_pts_us !== first.pts_us ||
    coverage.last_end_pts_us !== last.pts_us + last.duration_us ||
    coverage.start_t_us > first.drive_t_us ||
    coverage.end_t_us < last.drive_t_us
  ) {
    return 'The frame points do not match the declared coverage.'
  }
  return undefined
}

function lastIndexAtOrBefore(
  points: MediaSyncPoint[],
  value: number,
  select: (point: MediaSyncPoint) => number,
): number {
  let low = 0
  let high = points.length - 1
  let result = -1
  while (low <= high) {
    const middle = Math.floor((low + high) / 2)
    if (select(points[middle]) <= value) {
      result = middle
      low = middle + 1
    } else {
      high = middle - 1
    }
  }
  return result
}

export function frameAtVideoPts(
  index: MediaSyncIndex,
  ptsUs: number,
): MediaSyncPoint | undefined {
  if (!Number.isFinite(ptsUs) || !index.points.length) return undefined
  const pointIndex = lastIndexAtOrBefore(index.points, ptsUs, (point) => point.pts_us)
  if (pointIndex < 0) return undefined
  const point = index.points[pointIndex]
  const isLastEnd =
    pointIndex === index.points.length - 1 &&
    ptsUs === point.pts_us + point.duration_us
  return ptsUs < point.pts_us + point.duration_us || isLastEnd ? point : undefined
}

export function frameNearestDriveTime(
  index: MediaSyncIndex,
  driveUs: number,
): MediaSyncPoint | undefined {
  if (
    !Number.isFinite(driveUs) ||
    !index.points.length ||
    driveUs < index.coverage.start_t_us ||
    driveUs > index.coverage.end_t_us
  ) {
    return undefined
  }
  const beforeIndex = lastIndexAtOrBefore(index.points, driveUs, (point) => point.drive_t_us)
  if (beforeIndex < 0) return index.points[0]
  if (beforeIndex === index.points.length - 1) return index.points[beforeIndex]
  const before = index.points[beforeIndex]
  const after = index.points[beforeIndex + 1]
  return driveUs - before.drive_t_us <= after.drive_t_us - driveUs ? before : after
}

export function adjacentFrame(
  index: MediaSyncIndex,
  driveUs: number,
  offset: -1 | 1,
): MediaSyncPoint | undefined {
  const current = frameNearestDriveTime(index, driveUs)
  if (!current) return undefined
  const currentIndex = lastIndexAtOrBefore(
    index.points,
    current.segment_frame_id,
    (point) => point.segment_frame_id,
  )
  const targetIndex = currentIndex + offset
  return targetIndex >= 0 && targetIndex < index.points.length
    ? index.points[targetIndex]
    : undefined
}

export function mediaItemAtDriveTime(
  items: MediaManifestItem[],
  driveUs: number,
): { item: MediaManifestItem; index: number } | undefined {
  const playable = items
    .filter(
      (item) =>
        item.start_t_us != null &&
        item.duration_us != null &&
        item.duration_us > 0,
    )
    .sort((left, right) => {
      const startDifference = (left.start_t_us as number) - (right.start_t_us as number)
      return startDifference || left.segment_number - right.segment_number
    })
  const itemIndex = playable.findIndex((item, index) => {
    const startUs = item.start_t_us as number
    const endUs = startUs + (item.duration_us as number)
    return driveUs >= startUs && (
      driveUs < endUs ||
      (index === playable.length - 1 && driveUs === endUs)
    )
  })
  return itemIndex >= 0 ? { item: playable[itemIndex], index: itemIndex } : undefined
}

export function sortedPlayableMedia(items: MediaManifestItem[]): MediaManifestItem[] {
  return items
    .filter(
      (item) =>
        item.start_t_us != null &&
        item.duration_us != null &&
        item.duration_us > 0,
    )
    .sort((left, right) => {
      const startDifference = (left.start_t_us as number) - (right.start_t_us as number)
      return startDifference || left.segment_number - right.segment_number
    })
}

export function playbackTimeForAvailableMedia(
  items: MediaManifestItem[],
  driveUs: number,
): number {
  if (driveUs !== 0) return driveUs
  const firstStartUs = sortedPlayableMedia(items)[0]?.start_t_us
  return firstStartUs != null && firstStartUs > 0 ? firstStartUs : driveUs
}

function mediaManifestSignature(manifest: MediaManifest): string {
  return JSON.stringify({
    drive_id: manifest.drive_id,
    camera: manifest.camera,
    synchronized: manifest.synchronized,
    items: manifest.items.map((item) => ({
      segment_number: item.segment_number,
      start_t_us: item.start_t_us,
      duration_us: item.duration_us,
      artifact_id: item.artifact_id,
      url: item.url,
      mime_type: item.mime_type,
      codec: item.codec,
      fps: item.fps,
      sync_mode: item.sync_mode,
      sync_url: item.sync_url,
      frame_index_artifact_id: item.frame_index_artifact_id,
      frame_index_url: item.frame_index_url,
      sync_reason: item.sync_reason,
      video_sha256: item.video_sha256,
      timeline_origin: item.timeline_origin,
    })),
  })
}

export function mediaManifestEqual(
  current: MediaManifest,
  next: MediaManifest,
): boolean {
  return mediaManifestSignature(current) === mediaManifestSignature(next)
}
