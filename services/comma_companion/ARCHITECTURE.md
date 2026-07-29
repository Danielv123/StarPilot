# Comma Companion architecture

Comma Companion is a private archive, device-control plane, and driving-model
workbench for StarPilot. The server is intentionally isolated under
`services/comma_companion`; the comma-side integration is limited to transport
adapters.

## Boundaries

```text
comma
  |
  `- private resumable archive agent
                  |
                  v
       versioned ingest/control API
                  |
        +---------+------------------------+
        |                    |             |
        v                    v             v
             immutable raw files      SQLite catalog         live device state
                  |                        |                       |
                  v                        v                       v
             AV1 worker              telemetry index          web dashboard
                  |                        |
                  +--------------+
                                 v
                    model-counterfactual worker
```

- `API_HOST` is not changed. The ordinary comma/Konik uploader remains
  independent.
- `ATHENA_HOST` is not changed. Existing comma/Konik remote connectivity keeps
  working independently.
- Reliable archival uses a small outbound-only agent with a private journal,
  content hashes, resumable chunks, and safe typed commands. It never reuses
  the stock `user.upload` xattr.
- The agent is installed under `/data/private-pond-agent`, outside the
  replaceable `/data/openpilot` tree. A short fail-open hook in
  `/data/continue.sh` materializes its unit under volatile `/run/systemd/system`
  at boot. Agent failure must never block StarPilot startup.
- The server never imports live control processes. Rlog parsing and dynamics
  replay are versioned adapters with explicit inputs, outputs, provenance, and
  model-hash allowlists.
- Browser code only talks to the server. It never connects directly to the
  comma.

## Storage

The live deployment keeps mutable database state on the VM's local disk and
large content on the SMB mount:

```text
/var/lib/comma-companion/
  companion.sqlite3
  sessions/

/archive/comma-companion/
  objects/sha256/ab/cd/<digest>
  uploads/<upload-id>.part
  derived/<device>/<route>/<segment>/<camera>.av1.webm
  telemetry/<device>/<route>/v1/
  thumbnails/<device>/<route>/
```

Raw artifacts are immutable and content-addressed. Route/segment records point
to objects rather than owning copies. SQLite runs in WAL mode and must not live
on CIFS/SMB.

Raw video deletion is disabled by default. If enabled later, a source may be
removed only after:

1. its declared size and SHA-256 are verified;
2. the AV1 output was atomically finalized;
3. `ffprobe` validates codec, duration, and decodability;
4. the derived artifact is recorded durably; and
5. the configured grace period has elapsed.

Rlogs, qlogs, metadata, telemetry indexes, and model provenance are retained.

## Upload protocols

### Resumable agent and historical importer

1. `POST /api/v1/uploads` declares device, relative path, size, mtime, and
   optional SHA-256. The response contains an opaque upload ID and current
   offset.
2. `HEAD /api/v1/uploads/{id}` returns `Upload-Offset` and `Upload-Length`.
3. `PATCH /api/v1/uploads/{id}` accepts a bounded binary chunk with
   `Upload-Offset`.
4. Finalization verifies length and SHA-256, atomically installs the object, and
   returns a durable acknowledgement.

Chunks are idempotent at a particular offset. A mismatched offset returns
`409`. Authentication uses a per-device bearer token. Desktop historical
imports use a separate import token.

The agent never treats `.lock` absence as proof that a segment is complete. It
also requires stable file metadata across a grace interval and either a later
segment or a stopped logger. It re-stats after hashing and upload. Complete
rlogs/qlogs are decompressed through their terminal event where available;
stable crash-truncated files are retained and marked partial.

Completed source files are hard-linked into the agent's private spool before
upload. This protects the archived inode from ordinary log-root cleanup without
altering the source directory or stock uploader metadata. Spool pressure is
reported explicitly and has a separately configured emergency policy.

## Jobs

The single-container deployment uses a durable SQLite job table and one worker
process. This matches the current 2-vCPU/4-GB server. Job types are:

- `verify_artifact`
- `extract_telemetry`
- `transcode_video`
- `thumbnail`
- `route_finalize`
- `simulate_counterfactual`

Jobs have leases, bounded retries, progress, cancellation, and an error record.
The API process does not perform transcoding or rlog parsing in request
handlers.

## Video

Each camera segment is transcoded independently so a drive becomes viewable
before the complete route finishes:

```text
ffmpeg -r 20 -f hevc -i input.hevc \
  -map 0:v:0 -an -c:v libsvtav1 -preset 10 -crf 38 -b:v 0 \
  -maxrate:v <input-derived-cap> -bufsize:v <twice-the-cap> \
  -pix_fmt yuv420p -g 80 -cues_to_front 1 -f webm output.av1.webm
```

Elementary raw HEVC is forced to the camera's actual 20 fps; probing some raw
streams otherwise reports 25 fps and breaks synchronization. The final image
must provide `libsvtav1`; there is no `libaom-av1` fallback. The deployed
defaults are preset 10, CRF 38, 8-bit 4:2:0, two logical processors, and one
worker job. Qcamera MPEG-TS input preserves its single AAC/Opus track as
constrained-VBR Opus at 32 kbit/s.

Lower bitrate is a deterministic publication contract. The worker derives the
source's total average bitrate from bytes and validated effective duration,
then caps the first AV1 attempt to a `4/5` total-rate budget after reserving
125% of the configured Opus rate and the greater of 8 kbit/s or 5% for WebM
overhead. It fully validates and decodes the result. A result that is not both
strictly lower average bitrate and smaller in bytes is discarded and retried
once with a `3/5` budget. If that attempt also misses, no derived artifact is
published and the immutable raw input remains retained. The worker records the
integer-duration and rational-rate proof in result metadata, and the backend
revalidates it before cataloging.

Publication is crash-recoverable. A durable hidden journal containing the
source/encode contract plus every staged and final path, size, and SHA-256 is
created before the first rename; metadata is renamed last. A retry rolls back
only hash-verified journal leftovers whose filesystem identity is unchanged
immediately before unlink, removes a leftover journal-stage name only when it
still aliases the journal inode, fails closed on corrupt, replaced, or
unrelated files, and can adopt a fully validated generation under a
replacement durable job ID. The production storage preflight requires
hardlinks in each derived-output directory used by this no-replace journal
protocol.

Derived paths include a SHA-256 generation fingerprint over the immutable
source digest, canonical camera/input identity, encode profile, media contract
versions, and bitrate policy. A corrected source or changed CRF/profile
therefore creates a new generation instead of colliding with or silently
overwriting an older backup; a replacement job for the same generation reuses
the same paths.

HTTP Range responses serve the completed server-side AV1 backup; the UI never
falls back to media on the comma.

Video synchronization uses `roadEncodeIdx`, `wideRoadEncodeIdx`, and
`driverEncodeIdx`. Every WebM has an exact frame-index sidecar keyed by
`(camera, segment_num, segment_frame_id)` with integer PTS, duration, keyframe,
and time-base fields. The telemetry index joins that key to `logMonoTime`; it
does not infer time solely from `segment * 60`.

## Telemetry contract

The rlog adapter parses a complete route sequentially and carries `carParams`
and latest service state across segment boundaries. Its stable output uses
route-relative integer microseconds and SI units.

Default signals include:

- speed and acceleration;
- steering angle, signed rate, driver/EPS torque;
- desired and actual lateral acceleration and jerk;
- controller command, applied torque, and P/I/D/F terms;
- lateral-active, saturation, driver-overlay, and live-tune-valid flags;
- GPS trace, alerts, engagement state, and encoder time mappings.

`GET /api/v1/drives/{id}/series` accepts a time window, signal IDs, and
`max_points`. Min/max-envelope downsampling preserves spikes.

## Dynamics replay

The first supported counterfactual is the promoted Ioniq 5 neural lateral plant
with an allow-listed SHA-256. The model requires three seconds of clean history,
defaults to a one-second prediction, and is capped at its validated two-second
horizon.

Every result contains separate traces for:

1. recorded actual response;
2. model replay using the baseline/current controller; and
3. candidate-tune counterfactual response.

It also contains desired path, commands, ensemble disagreement, phase metrics,
validity flags, model hash, StarPilot commit, extractor version, rlog hashes,
and the complete parameter request. Candidate output is labelled as a model
estimate. Dirty, discontinuous, saturated, driver-overlay, identity-mismatched,
low-speed, or out-of-distribution windows are rejected or prominently flagged.

The controller kernel must be shared with runtime code before the UI claims
exact controller fidelity. Until then the mode is explicitly named
`approximate_closed_loop`.

## Authentication and control safety

- UI login uses an HttpOnly, Secure, SameSite=Strict signed session.
- Device and importer tokens are independent from administrator credentials.
- Mutations validate `Origin`, require an idempotency key, and write an audit
  event.
- Dangerous actions require a fresh password confirmation.
- The server and comma independently enforce command allowlists and offroad
  state.
- No browser endpoint exposes arbitrary shell commands or generic Params
  writes.
- The control protocol has no shell, arbitrary Params, CAN injection, updater,
  or SSH-proxy operation.
- Secrets, upload authorization headers, JWTs, and signed URLs are redacted
  from logs.

## API conventions

- All product APIs live under `/api/v1`.
- IDs are opaque.
- Timestamps are UTC ISO 8601; drive timeline coordinates are integer
  microseconds (`t_us`).
- REST is authoritative. `/api/v1/events` sends sequenced WebSocket
  invalidations and live progress.
- Long operations return `202` plus a job resource.
- The frontend receives simulator capabilities and parameter JSON Schema; it
  does not hardcode StarPilot tuning constants.
