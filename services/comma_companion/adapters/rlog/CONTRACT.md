# Rlog telemetry adapter contract

The adapter is a process boundary between StarPilot's evolving Cap'n Proto
schemas and the Comma Companion server. The server invokes it as a subprocess
and consumes stdout; it does not import `cereal` or StarPilot modules.

## Invocation

From a StarPilot checkout:

```powershell
uv run --no-project --with pycapnp==2.1.0 --with zstandard `
  python -m services.comma_companion.adapters.rlog `
  D:\comma_driving_logs\10.30.1.75\realdata `
  --route-id 000000dc--fe7070223b `
  --output telemetry.ndjson
```

Inputs may be a log root, a segment directory, or explicit files from one
route. For each segment, raw `rlog`, `rlog.zst`, and `rlog.bz2` are supported
in that preference order. `--log-type qlog` selects the corresponding qlog
names.

Stdout is NDJSON by default. Diagnostics only use stderr. `--format json`
wraps the same records in `{"records": [...]}` for small fixtures and manual
inspection. `chunk_size` is bounded to 16 through 16,384. File output is
written to a sibling temporary file and atomically replaced only after the
entire extraction succeeds; a failed extraction leaves an existing output
untouched.

## Versioning, identity, and record order

Every stream starts with `stream_header`, `signal_catalog`, and
`dynamics_catalog`, and ends with the authoritative `manifest` followed by
`stream_end`. Version 1 readers must ignore unknown fields and record kinds.

Scalar signals are asynchronous and signal-local:

```json
{"record":"series_chunk","signal":"vehicle.speed","tier":"full","chunk":0,"t_us":[0,10000],"v":[4.1,4.2],"log_mono_time_ns":["1230000000","1240000000"],"source_segment_num":[0,0],"source_ordinal":[40,52]}
```

The source timestamp is represented as a route-relative integer microsecond:

```text
t_us = floor((logMonoTime - origin_log_mono_time_ns) / 1000)
```

Nanosecond and absolute UTC values are decimal strings so JavaScript cannot
silently round them. Invalid floats are omitted; the adapter never emits JSON
`NaN` or infinity.

A relative `t_us` from a tail-only extraction is provisional: adding an
earlier segment changes the origin. The stable source identity is
`(route_id, source_segment_num, source_ordinal, log_mono_time_ns)`. Consumers
publish relative-time media, telemetry, and bookmarks atomically under the
final manifest's `timeline_version` and rebuild them together when that
version changes.

The adapter emits these additional record kinds:

- `frame_chunk`: exact encoder mappings for `road`, `wide`, `driver`, and
  `qcamera`.
- `model_path_chunk`: model path arrays and actions at model cadence.
- `dynamics_chunk`: deterministic causal rows for counterfactual replay.
- `marker`: intervals and transition points for onroad state, engagement,
  driver overlay, saturation, alerts, segment boundaries, gaps, and events.
- `manifest`: signal coverage, completeness, vehicle identity, UTC solution,
  warnings, source hashes, schema hashes, and extractor provenance.

## Camera synchronization

Frame rows retain the route encode ID, zero-based per-segment encoded-frame
ID, source frame ID, SOF/EOF timestamp, flags, and encoded byte length.
`segmentIdEncode` is not populated by loggerd, so `segment_encode_id` is null
and explicitly unsupported. Frame `t_us` uses camera EOF, with SOF as the
fallback; `event_t_us` separately retains the encode-index event time.

The transcode worker adds an output PTS mapping keyed by
`(camera, segment_num, segment_frame_id)`. `segment_frame_id` is loggerd's
zero-based source encoded-frame ordinal. A consumer must validate matching
camera/segment, contiguous source IDs, and a full one-to-one sidecar join. It
must not infer synchronization from decode occurrence or `segment * 60`.

Each `manifest.completeness.segments[]` entry contains nullable `start_t_us`,
`start_time_source`, and `camera_ranges_us`. The start uses the first road
camera EOF when available, otherwise the earliest camera EOF. It remains null
without encoder timing.

## Causal dynamics rows

`dynamics_chunk.rows[]` uses schema `comma-companion.dynamics-row` version 1.
It evaluates an absolute `logMonoTime` grid every 10 ms and emits every grid
row with exact causal inputs. The selected `carState` observation remains in
`source_t_us` and `log_mono_time_ns`; `source_time_error_us` exposes scheduler
jitter.

The alignment is `timestamp_causal_recorded_history_asof`. Each required fast
source (`carState`, `carControl`, `carOutput`, and `controlsState`) is selected
independently as the maximum valid source identity at or before the grid tick.
Each must be at most 35 ms old. It never interpolates, repeats qlog samples, or
admits a future source. An invalid `carState` is dropped without erasing the
prior valid state; an invalid `carControl`, `carOutput`, or `controlsState`
invalidates that source until another valid event arrives. Signed steering
rate is the causal grid difference of zero-order-held steering angle and is
zero on the first row after every continuity break.

Rows include the plant-training fields, raw unsigned `steering_rate_deg`,
applied-torque source, live/base/effective torque context, exact event
identities, and controller diagnostics. Applied torque is only
`carOutput.actuatorsOutput.torque`; there is no fallback to requested torque.
`controller_i_timing` is `post_update_asof_source_row`.

The effective torque context is bound when the selected `controlsState`
arrives. Valid nonempty `starpilotPlan.starpilotToggles` snapshots take
precedence. Invalid, malformed, or empty plan snapshots are not trusted. For
reviewed source commits, a versioned initData evaluator can instead reproduce
the StarPilot tuning-level and ForceAutoTune/ForceAutoTuneOff logic; a valid
causal `liveTorqueParameters.useParams` snapshot is used when it is stronger
evidence than an empty initData Params cache. Effective factor, offset, and
friction are stateful exactly like `controlsd`: if no update is requested,
the last applied values and their exact source identities are retained.
Extraction gaps reset that state to unknown.

Controller selection is modeled separately because `controlsd` chooses the
lateral controller once at process initialization. Later toggle observations
can prove consistency but never relabel historical rows. CarParams and
initData are likewise immutable process-initialization inputs: the earliest
valid available route snapshot owns the configuration, while its
`logMonoTime` remains the evidence-capture time and later duplicates are
consistency evidence. Unknown source commits,
contradictory controller observations, unresolved NNFF asset inventory, or
missing pre-extraction state fail closed.

Reviewed Ioniq 5 controller profiles are selected only by the exact StarPilot
source commit. Each row carries the profile ID, canonical 39-field parameter
SHA-256, source commit, value-space identifier, and vehicle factor multiplier.
The manifest independently aggregates source-age, controller-selection,
effective-torque-context, and controller-profile proofs from serialized rows.
`causal_input_eligible` is true only when the route is an rlog, at least one
row exists, all four proofs verify, and carParams/controller/source-commit
identity is unique.

Future feedforward is exposed in two forms:

- `gravity_adjusted_future_lateral_accel` is
  `controlsState.desiredCurvature * vEgo^2 - roll * g * speed_fade`.
- `future_feedforward_lateral_accel` additionally subtracts the effective
  lateral-acceleration offset times the same fade.

The fade is linear from 0 at 0.5 m/s to 1 at 2.5 m/s. Missing inputs produce
null plus `future_feedforward_eligible=false`; the extractor does not
substitute the controller setpoint. `future_feedforward_exact` is false
because logs do not prove every runtime toggle and internal controller state.

The final manifest repeats the telemetry provenance: schema/version,
alignment, `causal_input_eligible`, and `extractor_source_sha256`. Replay must
separately require a model artifact trained with the same alignment.
`--log-type qlog` is visualization-only because core control services are
decimated to roughly 10 Hz; no dynamics rows are synthesized.

## Downsample tiers

The adapter emits `full`, `100ms`, `500ms`, `2s`, and `10s` tiers directly
from full samples in fixed buckets anchored at route zero. Numeric buckets
keep first, earliest minimum, earliest maximum, and last. Boolean, enum, and
text buckets keep first, transitions, and last. Source order breaks ties, so
identical input bytes produce deterministic records. Every retained tier
sample carries the same stable source identity fields as the full sample.

## Completeness and causal state

Segments are processed by number. Service state is carried only across a
boundary proven contiguous by adjacent numbers, an
`endOfSegment`/`startOfSegment` sentinel pair, successful parsing, and a sane
monotonic-time gap. Missing, corrupt, failed, or noncontiguous boundaries
close intervals and clear causal service state.

Adjacent segment overlap is merged in global order
`(logMonoTime, segment_num, source_ordinal)`. Only byte-identical cross-segment
duplicates at the same timestamp/service are removed. Overlap is bounded to
5 seconds, two retained payloads, and the declared byte limits; non-adjacent
regression is rejected.

A route is `complete` and `publication_ready` only when:

- supplied segments are exactly contiguous from segment zero;
- segment zero starts with `startOfRoute`;
- intermediate segments end with `endOfSegment`;
- later segments start with `startOfSegment`;
- the final segment ends with `endOfRoute`; and
- no segment failed, was truncated, or failed frame-index validation.

An arbitrary tail such as segment 99 with a valid `endOfRoute` remains
`partial`; its origin is provisional. Terminal sentinel signal values are
retained as evidence.

The manifest reports missing numbers, failed/truncated logs, absent services
and frame maps, UTC quality, coverage, per-segment message counts, and SHA-256
of every source log. Historical custom-schema decode failures are counted by
segment and service with a deterministic warning instead of unstable parser
text.

`manifest.route_summary` reports distance from trapezoidal integration of
absolute valid `vEgo` over monotonic intervals no longer than 250 ms, plus the
first and last valid GPS fix formatted as seven-decimal `latitude,longitude`.
It includes the exact method names and included/excluded interval counts.

Provenance hashes the full wire-serialized `carParams`, its readable summary,
the allowlisted controller-related `initData.params` snapshot, cereal schemas,
and the sorted top-level Python files in the extractor package. It also records
repository dirty state and an optional build ID. The public extractor version
and package source SHA-256 must match the backend/model artifact allowlist.

The parser reads and hashes each source through the same open file descriptor,
then rewinds that descriptor for decompression to avoid path-swap ambiguity.
It enforces per-message, Cap'n Proto segment, message-count, compressed,
decompressed, route, retained-overlap, output-record, and output-byte limits.
Exact reviewed input/profile proofs do not claim full controller-internal
state; replay surfaces the remaining limitations explicitly.
