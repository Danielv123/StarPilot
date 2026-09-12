# Comma Companion device agent

This is the only component installed on the comma. It is a small, static Go
binary with no StarPilot or openpilot imports. It lives under
`/data/private-pond-agent`, outside the update-swapped `/data/openpilot` tree,
and makes outbound HTTPS requests only. A non-blocking process lock prevents
two Linux instances from writing the journal or spool concurrently.

## Journal memory and local profiling

Journal snapshots are read and written one map entry at a time. This avoids
holding a second complete JSON document alongside decoded history, and avoids
the full-document allocation when checkpointing. The version-4 schema, atomic
replace/fsync behavior, WAL replay and deep-copy isolation remain unchanged;
historical manifests are retained. Decoded history still consumes memory, so
this is not a constant-memory archive store.

The opt-in `TestJournalMemoryProfile` uses `COMMA_JOURNAL_BENCHMARK` as a
read-only source, copies it to a temporary directory, and measures opening,
five updates, checkpointing and reopening. Run it with `GOMEMLIMIT=160MiB`:

```sh
COMMA_JOURNAL_BENCHMARK=/path/to/copied-journal.json GOMEMLIMIT=160MiB \
  go test ./internal/journal -run '^TestJournalMemoryProfile$' -v -count=1
```

`TestAgentMemoryProfile` additionally exercises agent startup reconciliation,
command recovery, scans and heartbeat generation. It requires
`COMMA_BENCHMARK_ISOLATED=1` and must run in a disposable container with no
device/archive mounts or external network. It uses no real credentials and
does not start upload or command loops. Compile the test binary first, then
run it with a 256-MiB memory limit, swap disabled and `GOMEMLIMIT=160MiB`.
Do not set this isolation flag on the comma itself.

On a copied 64-MiB journal with 1,591 route inventories, the original agent
was OOM-killed during startup under that limit. The streaming version
completed startup, scans and checkpointing with a 153-MiB peak RSS and no
OOM kills in the local Linux simulation. The journal-only Windows test
reduced peak Go-managed memory from 253 to 149 MiB and cumulative allocations
from 1,635 to 384 MiB. These are local reproduction results, not an on-road
memory guarantee; cgroup totals also include reclaimable file cache.

## Safety and retention model

- A file is never queued while its segment has a `.lock`.
- Size and mtime must be unchanged for at least two observations **and** the
  configured `stable_duration`. A segment also needs a newer segment or a
  final/offroad grace period. Rapid remote rescans cannot bypass the elapsed
  time gate.
- Offroad uploads require complementary `IsOffroad` and `IsOnroad` values and
  the configured stability window. `offroad_max_age` defaults to `0s`, which
  disables wall-clock expiry so data accumulated through multi-day offline
  periods is still uploaded. A positive value can opt back into an age limit.
- The agent never reads or writes the stock `user.upload` xattr and never
  removes files from a logging root.
- Before hashing or uploading, it creates a hardlink in the private spool.
  This preserves the inode if the stock deleter removes the original path.
  The spool and logging roots therefore have to be on the same filesystem.
- The source is re-statted after SHA-256 hashing. If it changes, the journal
  first records any required server-session cancellation; only then is the
  local link released and the changed source returned to observation.
- A spool link is removed normally only after the server reports a durable,
  length- and SHA-256-verified object.
- The example configuration keeps at least 8 GiB free and at most 12 GiB
  protected, then pauses new spooling instead of deliberately abandoning
  retained data. This preserves existing hardlinks but can exhaust the device
  filesystem during a long outage. The optional `release_oldest` policy
  prioritizes device availability: it releases old non-durable hardlinks
  without deleting source paths and marks matching, still-present sources for
  later respooling after the old server session is canceled. A `released`
  journal record and counter expose every such event.
- Startup reconciliation walks a bounded active-spool namespace. Known
  durable/released links are retried for cleanup, known non-durable links are
  validated or adopted, and unknown links are moved to a reported quarantine
  without deletion. A truncated active-spool reconciliation blocks new
  spooling until the namespace is reviewed and the agent is restarted.
  Quarantine bytes count against the retention ceiling, and a truncated
  quarantine scan also blocks new spooling; review either condition manually.
- Terminal journal records are compacted only after both the source and spool
  paths are gone and no server cancellation remains. This bounds long-term
  journal growth without allowing a still-present logger file to be uploaded
  again.

The queue favors qlog, rlog, qcamera, road, wide, driver, then other files.
Each default eight-file hashing batch reserves one slot for media, and every
sixteenth upload selection is oldest-first across all tiers, so large media
continues to progress.

## Build

From Windows PowerShell:

```powershell
.\build.ps1 -Version 0.1.0
```

The build is pinned to the reviewed Go 1.26.5 toolchain and fails if a
different compiler is selected. It runs pinned govulncheck 1.6.0, all tests,
disables CGO, and produces
`dist/comma-companion-agent-linux-arm64`. It also builds
`dist/comma-companion-control-helper-linux-arm64` as an inactive review
artifact. The installer does not copy, register, or start that helper.

## Device installation and ownership

For the first install:

1. Copy this directory to a device-side staging directory outside any Git
   worktree.
2. Copy `config.example.json` to `config.json` and set the server and device
   ID. Do not put a token in the JSON.
3. While the comma is confirmed offroad, make the staged agent executable and
   run the read-only stream discovery. Paste its `expected_streams` value into
   `config.json`; activation deliberately refuses an empty profile:

   ```sh
   chmod 0555 dist/comma-companion-agent-linux-arm64
   ./suggest-streams-device.sh ./config.json 4
   ```

   Logging roots are alternatives, not simultaneous authorities. Configure
   every discovered root profile with an rlog, but only the root actually
   present for a route is applied.

   When openpilot's `RecordFront` parameter is enabled, merge the checked-in
   `inventory.openpilot-all-cameras.json` profile into `config.json` instead
   of relying only on historical stream discovery. Discovery cannot advertise
   `driver` until at least one closed segment already contains
   `dcamera.hevc`. The checked-in profile marks driver video as
   `optional_until_observed`: routes recorded before front-camera capture was
   enabled remain complete, while a route that contains driver video in any
   segment must contain it in every segment.
4. Put the separately generated bearer token in `device-token`, with no
   trailing commentary, and run `chmod 0600 device-token`.
5. Run the staging directory's `install-device.sh` as `comma`.

Never create or retain `device-token` in this repository or another Git
worktree. Root-level token and token-staging filename variants are ignored as
an additional guard, but ignore rules do not protect a credential that was
already committed. Rotate any token that ever entered Git history. After a
successful install or token rotation, the updater unlinks plaintext
`device-token` copies from both the staging and legacy agent locations; only
the root-owned persistent credential remains.

The installer validates the candidate config and Linux ARM64 ELF header,
builds an immutable content-addressed release, and atomically switches
`/data/private-pond-agent/current`. The candidate is not executed in the
installer's unsandboxed user context. It is started only through the hardened
unit, which reloads systemd and waits up to 55 seconds for the first
server-accepted heartbeat and five consecutive active seconds. A failed
activation reselects and restarts the previous release. Binary, config, and
unit therefore change as one release.

No finite local retention policy can guarantee every byte through an
arbitrarily long network outage. If the stock logger deletes an original after
the private spool has reached its configured bounds, that footage is
unrecoverable. Monitor `storage_pressure`, `spool_bytes`,
`spool_capacity_bytes`, `files_released_total`, and upload throughput, and
size the limits for the longest expected offline period.

Installed code, immutable releases, unit files, bootstrap scripts, and config
are root-owned and not writable by `comma`. Config is `root:comma` mode 0440.
The only agent-owned writable tree is
`/data/private-pond-agent/spool` (`comma:comma`, mode 0700). The installer never
recursively changes spool ownership because spool files can be hardlinks to
logger output.

The persistent credential is moved outside the agent tree to
`/data/private-pond-agent-secrets/device-token`, owned by root at mode 0600
inside a root-only 0700 directory. systemd `LoadCredential=` makes a separate
read-only runtime credential available to the `comma` service.
`COMMA_COMPANION_TOKEN_FILE` contains only that runtime file path; token bytes
are never placed in argv, the environment, or config.

The agent writes `/run/comma-companion-agent/ready` atomically only after the
server accepts a heartbeat. The updater clears this per-service runtime file
before restart and requires a new one, so an invalid token, rejected device,
unreachable server, or immediately crashing process triggers rollback instead
of passing a liveness-only check.

The installer preserves the first pre-hook `/data/continue.sh` at the fixed,
root-only path
`/data/private-pond-agent/backups/continue.sh.first-install`. It never
overwrites that backup. It inserts these two lines immediately after the
shebang:

```sh
# comma-companion-agent bootstrap
/usr/bin/timeout --kill-after=1s 8s /data/private-pond-agent/bootstrap.sh >/dev/null 2>&1 || true
```

At every boot, `bootstrap.sh` copies the unit into the volatile
`/run/systemd/system`, reloads systemd, and starts it. Each operation is
independently time-bounded and the script always exits successfully, so an
agent failure cannot block StarPilot startup. The unit runs as `comma`, at idle
I/O priority, with CPU and memory limits. `ProtectSystem=full`,
`NoNewPrivileges=true`, and an empty capability set protect the process.
The high-impact `/data/openpilot`, `/data/continue.sh`, and `/data/params`
paths are read-only in the service namespace. Logging roots and spool are
deliberately not placed behind separate systemd bind mounts: putting those two
hardlink endpoints on different mount objects can make the required link
return `EXDEV`. Root ownership and file modes make the installed private tree
read-only to `comma`, with only its spool writable, while preserving the
same-mount hardlink guarantee.

The default policy permits uploads while onroad and over cellular, but still
requires the active network to be reported as unmetered. This matches the
device UI's cellular metering choice without allowing uploads on a connection
the device considers metered.

When `require_wifi` is enabled as an optional stricter policy, `wlan0` must be operational and every
lowest-metric IPv4/IPv6 default route must use that interface. A higher-metric
cellular fallback therefore does not block uploads, but a selected or
equal-cost cellular route does. A Params value that merely says `wifi` cannot
override the route check, and the policy is checked again immediately before
every upload chunk.

The optional Wi-Fi-only mode is a fail-closed route gate, not a privileged socket binding. There is a
small race if the kernel changes its selected route after the per-chunk check
but before or during the HTTP request. At most one configured chunk (8 MiB
by default) can already be in flight when that happens. Enforcing
`SO_BINDTODEVICE` would remove that race but would require granting the agent
an additional Linux capability; the supplied service deliberately runs without
it.

Normal StarPilot overlay updates do not replace the agent. A full UI reinstall
can rewrite `/data/continue.sh`; after one, run
`/data/private-pond-agent/restore-device.sh` to reinstall the hook and verify
the retained release. If an overlay swaps `/data/openpilot` without rebooting,
the same restore command refreshes the service's read-only checkout view used
for branch/commit statistics.

### Updating, rollback, and reversible removal

After the initial install, the canonical directory is root-owned. Copy each
new trusted bundle to a separate staging directory, then run its updater.
Remote commands cannot invoke any deployment script.

```sh
bash /data/private-pond-agent-update-20260729/update-device.sh
```

Omit `config.json` from the staged bundle to retain the current config. To
rotate the credential, put the new value in the staged `device-token`, set it
to mode 0600, and use:

```sh
chmod 0600 /data/private-pond-agent-update-20260729/device-token
bash /data/private-pond-agent-update-20260729/update-device.sh --replace-token
```

Remove the separate staging directory after verifying the update. The updater
has already unlinked its exact `device-token` path. Re-running an update with
identical binary, config,
and unit content reuses the same immutable release. The following operations
are idempotent:

```sh
/data/private-pond-agent/rollback-device.sh
/data/private-pond-agent/uninstall-device.sh
/data/private-pond-agent/restore-device.sh
```

Rollback selects the recorded prior release and restores the original release
if the target is unhealthy. Uninstall removes only the known boot-hook lines
and volatile systemd unit; it deliberately retains releases, config,
credential, journal, and spool. Restore re-adds the exact hook and starts the
retained current release.

Run `bash deployment-static-test.sh` from a bundle to syntax-check all device
scripts and assert the credential, systemd, filesystem, and rollback
invariants.

### Remote-command security

The example disables every disruptive command. A production config may
explicitly enable only `restart_agent`; systemd then performs the actual
restart under the same unprivileged unit and sandbox. The updater rejects
`allow_starpilot_restart=true` and `allow_power_commands=true`.

The service sets Go's soft heap limit to 160 MiB beneath systemd's 192 MiB
memory-high and 256 MiB hard limit. Journal reads use immutable generations so
heartbeat and upload selection do not duplicate the complete journal. When
storage pressure blocks new spool links, full-backlog scans remain read-only
and do not rewrite the stability journal every scan interval.

StarPilot restart, reboot, and shutdown are unavailable in this release. They
are not advertised in heartbeat capabilities, and no root helper service,
credential, socket, or panda-state producer is installed. The source tree
contains a staged fixed-operation helper solely for review. Its reboot path
uses manager's deferred `DoReboot` parameter, and it would additionally
require two advancing, fresh, root-owned raw `pandaStates` proofs showing at
least one known panda with both ignition sources false, alongside two
unchanged offroad Params reads over ten seconds. With no certified proof
producer installed, it fails closed. A UI may show these rows only as
explicitly disabled and unavailable; it must not present an actionable
control. The current production agent never advertises or enables them.

The hardened archive service cannot use `sudo` because
`NoNewPrivileges=true` and its capability sets are empty. No general-purpose
privileged execution path is installed.

## Wire contract

Every request uses:

```http
Authorization: Bearer <per-device token>
User-Agent: comma-companion-agent/<version>
```

Production configuration must use HTTPS. IDs are opaque and timestamps are
UTC RFC 3339.

### Heartbeat and command poll

```http
POST /api/v1/devices/{device_id}/heartbeat?wait_seconds=25
Content-Type: application/json
```

```json
{
  "agent_version": "0.1.0",
  "timestamp": "2026-07-28T22:00:00Z",
  "state": "idle",
  "capabilities": [
    "resumable_upload_v1",
    "sha256",
    "hardlink_spool",
    "full_backlog_v1",
    "typed_commands_v1",
    "storage_guard_v1",
    "route_inventory_v1",
    "command_status",
    "command_rescan",
    "command_pause",
    "command_resume",
    "command_retry_upload",
    "command_cancel_upload"
  ],
  "metrics": {
    "pending_bytes": 0,
    "unuploaded_bytes": 0,
    "unuploaded_files": 0,
    "unuploaded_scan_at": "2026-07-28T21:59:50Z",
    "unuploaded_scan_complete": true,
    "spool_bytes": 0,
    "storage_free_bytes": 9999999999,
    "storage_pressure": false,
    "upload_bytes_per_second": 0
  },
  "offroad": true,
  "network_type": "wifi"
}
```

`pending_bytes` is the resumable remainder already represented in the upload
journal. `unuploaded_bytes` and `unuploaded_files` cover the complete recognized
logging tree, including files that have not entered the bounded protected spool.
The full-backlog values are refreshed by the normal filesystem scan and are a
lower bound when `unuploaded_scan_complete` is false.

The response is:

```json
{
  "server_time": "2026-07-28T22:00:00Z",
  "commands": [
    {
      "id": "0123456789abcdef0123456789abcdef",
      "device_id": "dongle-id",
      "type": "rescan",
      "state": "delivered",
      "issued_at": "2026-07-28T21:59:59Z",
      "expires_at": "2026-07-28T22:04:59Z",
      "requires_offroad": false,
      "args": {}
    }
  ]
}
```

The heartbeat `metrics.host` object is bounded and contains device uptime/load,
sampled CPU use, total/available memory, `/data` capacity, thermal-zone
temperatures, per-interface byte counters, generic power-supply readings,
current route, StarPilot branch/commit, and manager/loggerd presence. It never
enumerates processes or sends command lines. A local five-second minimum
interval applies even when a server returns immediately instead of honoring
the long-poll request.

The exact device allowlist is:

- `status`
- `rescan`
- `pause`
- `resume`
- `retry_upload` with `file_id`, `upload_id`, or `scope: "all"`
- `cancel_upload` with the same selectors
- `restart_agent` when explicitly enabled locally
- `restart_starpilot`, `reboot_device`, and `shutdown_device` are recognized
  only for forward-compatible rejection and are unavailable in this release

Command IDs must be 32 lowercase hexadecimal characters. The agent binds each
ID to its immutable type, arguments, issued time, expiry, and offroad
requirement. It accepts at most 32 commands per heartbeat, requires a lifetime
between 10 seconds and 24 hours, rejects unknown argument fields, and rejects
expired or materially future-issued commands locally.
When both `file_id` and `upload_id` are present they must match the same local
record; `scope: "all"` is exclusive and cannot be combined with either ID.

The example rejects `restart_agent` because its flag is false. A production
configuration can opt into that one capability. The three root-level actions
remain unadvertised and updater-rejected.

Duplicate command IDs return the persisted original result and do not execute
twice.

Results use a stable idempotency key. Agent restart is durably reported as
`running` before the process exits, then as `succeeded` only after the next
process startup proves the restart occurred. Other commands report the
terminal states `succeeded`, `failed`, or `rejected`:

```http
POST /api/v1/devices/{device_id}/commands/{command_id}/result
Idempotency-Key: command-result:{command_id}:{state}
Content-Type: application/json
```

```json
{
  "state": "succeeded",
  "started_at": "2026-07-28T22:00:01Z",
  "finished_at": "2026-07-28T22:00:01Z",
  "message": "scan requested",
  "error": ""
}
```

### Route inventory

Before declaring or uploading route content, the agent declares an immutable
route inventory:

```http
POST /api/v1/route-inventories
Idempotency-Key: route-inventory:{manifest_sha256}
Content-Type: application/json
```

The body contains `manifest_sha256` and `manifest`. The digest is lowercase
SHA-256 over canonical JSON with recursively sorted object keys, normalized
sorted arrays, and no insignificant whitespace. Generation 1 has no
predecessor; each changed or late-arriving file produces a new generation
whose `previous_manifest_sha256` names the prior immutable manifest. The
server must return either `201` for a new declaration or `200` for an exact
replay, echoing the digest and generation with `state: "accepted"`.

Each segment has one explicit `present` or `missing` row for every expected
role, formatted as `{root}|{artifact}|{camera-or--}`. A route is `complete`
only when it is closed, has no segment gaps or missing roles, has exactly one
active logging root, and that root has one configured rlog authority. More
than one active root is always `partial` with
`multiple_active_log_roots`; an unconfigured active root is also explicitly
partial. Files discovered after an earlier declaration supersede it rather
than mutating it.

### Resumable upload

Declaration uses a stable generation-scoped
`upload-create:{local_file_id}:{upload_attempt}` idempotency key. Ordinary
resume keeps the same attempt; an explicit retry, a server terminal failure,
or a missing server session advances it:

```http
POST /api/v1/uploads
Idempotency-Key: upload-create:{local_file_id}:{upload_attempt}
Content-Type: application/json
```

```json
{
  "file_id": "64-character-lowercase-sha256-file-id",
  "device_id": "dongle-id",
  "route_name": "0000011c--116efaac12",
  "segment_number": 4,
  "artifact_type": "video",
  "camera": "road",
  "relative_path": "realdata/0000011c--116efaac12--4/fcamera.hevc",
  "size": 74973184,
  "mtime_ns": 1785276000000000000,
  "sha256": "lowercase-hex",
  "completion_evidence": [
    "no_lock",
    "stable_duration",
    "newer_segment"
  ],
  "partial": false
}
```

`file_id` is the agent journal's immutable 64-character lowercase SHA-256
identifier. It remains the same across upload-session cancellation and
redeclaration, so an administrative `retry_upload` can still select the exact
local file after the older `upload_id` has been cleared.

The response contains both an opaque ID and the authoritative current offset:

```json
{
  "upload_id": "opaque-upload-id",
  "offset": 0,
  "length": 74973184,
  "state": "receiving",
  "durable": false
}
```

Resume state:

```http
HEAD /api/v1/uploads/{upload_id}
```

The response headers are `Upload-Offset`, `Upload-Length`, `Upload-State`,
`Upload-Durable`, `Upload-Terminal`, `Upload-Retry-Action`, and, after
verification, `Upload-SHA256`.

The production agent uses 8-MiB chunks. Per-chunk offsets are durably appended
to a small write-ahead log and replayed over the journal snapshot at startup,
so resumability does not require rewriting and syncing the complete journal
for every chunk. Graceful shutdown checkpoints the WAL into the base journal
before exit, preserving rollback compatibility with older agent releases. The
server continues to enforce a 16-MiB maximum:

```http
PATCH /api/v1/uploads/{upload_id}
Content-Type: application/offset+octet-stream
Upload-Offset: 0
Upload-Length: 74973184
Upload-Checksum: sha256 <base64 SHA-256 of this chunk>
Content-Length: 8388608
```

An offset mismatch returns `409`; the agent reconciles through `HEAD`.
Completion is not inferred from a full offset. The spool link remains until a
response or subsequent `HEAD` reports the exact complete length, provides a
non-empty SHA-256 equal to the local digest, and either says `durable=true` or
uses the terminal state `complete`/`durable`.

Failed and canceled upload sessions are terminal and are never reused. Before
redeclaring, the agent durably queues:

```http
POST /api/v1/uploads/{upload_id}/cancel
Idempotency-Key: upload-cancel:{local_file_id}:{upload_id}
```

Only an acknowledged cancellation (or authoritative 404) clears the old
session ID. One terminal automatic redeclaration is allowed; another terminal
failure requires an explicit `retry_upload`, preventing an unbounded
generation loop. `cancel_upload`, storage-pressure release, and changed-file
invalidation all use the same persistent cleanup queue.
