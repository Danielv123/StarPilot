# Comma Companion media worker

This package is the server-only media boundary. It has no StarPilot imports and
does not alter, read from, or fall back to a comma after an artifact has been
ingested. It converts one completed camera artifact at a time to a
browser-streamable AV1 WebM, validates it, and publishes each output with an
atomic rename.

## Runtime behavior

- `input.input_format` is a required, backend-derived enum. `raw_hevc` always
  forces the HEVC elementary-stream demuxer; `mpegts` always forces the MPEG-TS
  demuxer. FFprobe/FFmpeg autodetection is never used for ingested bytes.
- The accepted canonical mappings are road/fcamera, wide/ecamera,
  driver/dcamera, and qcamera/qcamera as `raw_hevc`, plus qcamera/qcamera as
  `mpegts`. The backend derives this value from the exact canonical artifact
  name; callers must not infer it from user-supplied content or MIME metadata.
- Raw HEVC is interpreted at 20 fps. Camera files can otherwise be misreported
  as 25 fps because the elementary stream has no container timestamps.
- The only encoder is `libsvtav1`. Defaults are preset 10, CRF 38, two logical
  processors, one worker job, 8-bit 4:2:0, and a four-second keyframe interval.
- Lower bitrate is an enforced output contract, not an assumption about CRF.
  The worker calculates the source's total average bitrate from its byte count
  and validated effective duration (exact frame count / forced rate for raw
  HEVC, integer microseconds for MPEG-TS), then applies capped CRF with an
  initial total target of `4/5` of the source rate. The AV1 `maxrate` is that
  total budget after reserving 125% of the configured Opus rate and the greater
  of 8 kbit/s or 5% for WebM overhead. Opus uses constrained VBR.
- After complete validation and decode, the WebM must be both smaller in bytes
  and strictly lower in measured total average bitrate than the source. If the
  initial attempt misses that gate, the staged output is discarded and retried
  once with a `3/5` total target. If that also misses, nothing is published and
  the raw input remains retained. Result metadata records the exact input and
  output bitrates, durations in microseconds, selected budget, and rational
  output/input ratio under `bitrate_policy`.
- A qcamera MPEG-TS must contain exactly one H.264 video stream and may contain
  one AAC or Opus audio stream. Accepted audio is preserved as Opus at
  32 kbit/s by default; `encode.preserve_audio` can disable it. Every raw HEVC
  input, including `qcamera.hevc`, must contain exactly one HEVC video stream
  and no audio or other streams.
- Every media read forces its expected demuxer and limits FFmpeg protocols to
  local `file` and `pipe`. A header/topology probe runs first under decoder and
  stream whitelists; only HEVC, H.264, AAC, Opus, AV1, and the configured AV1
  decoder can be opened for their respective formats.
- The decoded-frame scan is streaming and fail-closed. It checks every video
  frame's dimensions and every decoded audio frame's channel/timing metadata,
  stops after 10,000 video frames, 72,000 audio frames, or 83,024 packets, and
  has a non-configurable 120-second wall timeout. Captured process output is
  limited to 16 MiB, individual lines to 64 KiB, and reader queues are bounded.
- Other hard limits are 8 MiB of initial probe data, 5 seconds of format
  analysis, 4,096 initial probe packets, two streams, a 512-MiB single
  allocation, a 1-GiB input, 4,096x2,160 / 8,847,360 pixels, 180 seconds, and
  60 fps. Audio is limited to two channels and decoded rates of 8-48 kHz.
  Video/audio/container durations must agree within one second. Mismatches are
  rejected before encoding.
- Encoded WebM output is limited to 1 GiB by both FFmpeg and a live file-size
  watcher; each image is limited to 64 MiB and all poster/thumbnail images
  together to 256 MiB. Disk preflight reserves the full job-specific derived
  budget (plus 32 MiB for sidecars/metadata). Limit failures remove staged
  outputs.
- WebM seek cues are moved before the first media cluster without a fixed
  reserved-padding block. Validation rejects a
  missing AV1 stream, zero duration or frames, changed dimensions, lost frames,
  non-front-loaded cues, or a decode error.
- Poster and evenly spaced thumbnail JPEGs are generated from the completed
  server-side AV1 output.
- Every WebM gets an atomic frame-index sidecar containing the exact integer
  PTS, duration, keyframe flag, and 0-based source segment-frame ID for every
  decoded output frame. The index uses the WebM stream's own rational time
  base; it never derives time from `segment * 60`.
- Input and every published output receive a SHA-256 and byte count.
- A caller-supplied telemetry time-map reference is copied into the immutable
  result metadata. The worker supplies the exact media half of the join and
  never invents a missing rlog timestamp.
- Raw input is never deleted. `retain_raw` defaults to true and the result
  always records `raw_retained: true`; any future retention policy belongs to a
  separate catalog transaction after durable validation.

Temporary files are created beside their final targets, so `os.replace` stays
on the destination filesystem. The worker checks available space before
starting. A deadline covers hashing, probing, encoding, validation, and image
generation. Cancellation works through SIGINT/SIGTERM, a caller event, or the
optional `limits.cancel_file`.

The package expects FFmpeg built with `libsvtav1`. Worker concurrency belongs
to the durable job runner and should remain one on the current 2-vCPU/4-GB
server.

The complete workflow was exercised against a representative 1928x1208 raw
fcamera fixture under Docker limits of two CPUs and 3.5 GB. Its 130 frames
(6.5 s) completed in 17.667 s of worker time; the video fell from 8,093,878
bytes to 1,017,165 bytes and from 9,961,696 to 1,251,896 total average bit/s
(12.57% of the source rate) on the initial `4/5` budget. It retained exactly
20 fps/130 frames/6.5 s, had front-loaded cues, and passed a full decode. Its
15,564-byte sidecar contained all 130 frames in the `1/1000` WebM time base
with no inferred durations. This is a sizing check rather than a
quality-tuning result.

## Job contract

`job.schema.json` is the authoritative version 1 job contract, and
`frame-index.schema.json` defines the emitted sidecar. `example-job.json`
shows every job field. Output video paths must end in `.webm`.
`input.segment_num` is required because a frame index without segment identity
cannot be joined safely. `input.input_format` is also required and is echoed in
`result.input.input_format` so the backend can verify the worker consumed the
derived format. If `poster_path`, `frame_index_path`, or
`thumbnails_dir` is omitted, it is derived beside the video:

```text
road.av1.webm
road.av1.poster.jpg
road.av1.frames.json
road.av1.thumbnails/000.jpg
```

Unknown fields and unsupported schema versions are rejected. An existing
output is also rejected unless its metadata, hashes, frame index, bitrate
proof, source identity, encode profile, and time mapping prove the same media
generation is already complete. `encode.overwrite=true` is rejected because
replacing an older multi-file bundle cannot preserve the crash-safe publication
contract. A fully validated generation may be adopted by a new durable-job ID;
the worker atomically rewrites only the completion marker and returns
`already_complete`.

The backend adapter resolves its queued source artifact into an absolute
`input.path` and sets `input.artifact_id`. Only after an `event=result` record
with `status=complete` may it insert the catalog row for the derived media:

```text
kind              derived_video
camera            result.input.camera
codec             result.output.video.probe.codec_name
mime_type         result.output.video.mime_type
duration_us       round(duration_seconds * 1_000_000)
status            ready
source_artifact_id result.input.artifact_id
```

Device, drive, and segment IDs are inherited from the source artifact. The
backend converts `result.output.video.path` to an archive-relative
`storage_path`; the worker deliberately has no database imports.

It then catalogs `result.output.frame_index` after the derived-video row:

```text
kind               video_frame_index
camera             result.output.frame_index.camera
mime_type          application/json
status             ready
source_artifact_id derived-video artifact ID
```

The sidecar's version 1 shape is:

```json
{
  "schema_version": 1,
  "mapping_type": "encoded_frame_pts",
  "join_key": ["camera", "segment_num", "segment_frame_id"],
  "ordinal_basis": 0,
  "source_frame_key": "segment_frame_id",
  "camera": "road",
  "segment_num": 99,
  "source_artifact_id": "artifact_01JEXAMPLE",
  "video": {
    "path": "/archive/derived/road.av1.webm",
    "sha256": "..."
  },
  "time_base": {
    "numerator": 1,
    "denominator": 1000,
    "text": "1/1000"
  },
  "frame_count": 130,
  "first_pts": 0,
  "last_end_pts": 6500,
  "duration_inference_count": 0,
  "frames": [
    {
      "ordinal": 0,
      "segment_frame_id": 0,
      "pts": 0,
      "duration": 50,
      "pts_us": 0,
      "duration_us": 50000,
      "keyframe": true
    }
  ]
}
```

`segment_frame_id` is the 0-based per-segment encoder ordinal published as
`EncodeIndex.segmentId`; it is not `segment_encode_id`. The rlog adapter
preserves it in `frame_chunk`. A synchronized frame is therefore an exact join
on `(camera, segment_num, segment_frame_id)`. The backend should only mark a
media segment synchronized when source, decoded-video, sidecar, and joined
rlog counts agree. At playback time, binary-search the sidecar's half-open
`[pts_us, pts_us + duration_us)` intervals, then use the matching
`segment_frame_id` to obtain `frame_chunk.t_us`/`log_mono_time_ns`.

FFprobe packet/frame duration is retained when available. If it is absent, the
worker deterministically uses the following PTS delta and uses the prior
positive duration for the final frame; `duration_inference_count` exposes that
condition. Non-monotonic PTS, missing/reordered frames, overlaps, invalid time
bases, or a sidecar/video count mismatch reject the whole job.

Job paths and `input.input_format` are an internal trusted-worker contract, not
a public API. Every job input/output path must be an absolute local path. Before
launching the process, the backend must resolve the source under its immutable
object root, derive the format from the canonical artifact kind/name, and
resolve every destination under its configured derived-media root. The worker
canonicalizes paths before passing them to FFmpeg, independently verifies the
kind/camera/format tuple and stream shape, and repeats the local-protocol
whitelist for outputs. It never invokes a shell, but it intentionally does not
duplicate the server's tenant/path authorization policy.

The metadata JSON is the completion marker. Before the first output rename,
the worker durably creates a hidden publish-transaction journal beside it. The
journal binds the actual source hash and output contract to every exact stage
and target path, size, and SHA-256. On retry, an incomplete transaction is
rolled back only when every existing leftover matches that proof and retains
the same filesystem identity immediately before unlink; unrelated paths are
never removed. A crash between the no-replace hardlink creation and stage
unlink is recovered only by removing the journal-stage name that still aliases
the journal inode. A missing, malformed, mismatched, replaced, or corrupt
leftover fails closed as an output conflict. Metadata is still published last,
directory renames are synced, and the journal is removed only after the
complete bundle is durable. Production storage preflight requires hardlink
support in every worker output directory.

## CLI

Install the package or invoke it from its source tree:

```bash
python -m comma_companion_media_worker probe /archive/object --input-format raw_hevc
python -m comma_companion_media_worker encode job.json
python -m comma_companion_media_worker validate output.webm \
  --source input.hevc --source-input-format raw_hevc
```

`encode` writes newline-delimited JSON events to stdout. Progress events carry
the phase, frame, fps, encoded time, output size, speed, and a bounded
`fraction` when source duration is known. The final line is an `event=result`
record. Machine-readable errors go to stderr and use stable codes.

For direct source execution:

```bash
PYTHONPATH=services/comma_companion/media_worker/src \
  python -m comma_companion_media_worker probe input.hevc --input-format raw_hevc
```

## Tests

```bash
python -m pytest services/comma_companion/media_worker/tests
```

Unit tests do not require FFmpeg. End-to-end tests generate tiny raw HEVC and
H.264/AAC MPEG-TS fixtures, verify the AV1/Opus outputs, and confirm playlist
bytes containing HTTP or file URLs are rejected under the forced MPEG-TS
demuxer. They skip cleanly when the required FFmpeg tools or encoders are not
available.
