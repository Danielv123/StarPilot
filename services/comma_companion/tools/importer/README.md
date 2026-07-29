# Historical drive importer

This dependency-free Python 3.11 tool imports copied comma routes without
renaming, modifying, or deleting any source file. It understands both current
and legacy layouts:

- `<device>/realdata/<route>--<segment>/<artifact>`
- `realdata/<route>--<segment>/<artifact>`
- `<route>/<segment>/<artifact>`
- flattened `<route>--<segment>--<artifact>` files
- `realdata/boot/<boot-id>.zst`

Recognized logs include `rlog` and `qlog` with optional `.bz2`, `.zst`, `.gz`,
or `.xz` compression. Recognized camera aliases include `fcamera`/`camera`
(road), `ecamera` (wide), `dcamera` (driver), and `qcamera`, with legacy HEVC,
H.265, TS, MP4, or MKV containers. Other files are classified locally as
metadata, stats, bootlog, crash, user-flag, or `other` for selection, then use
the agent-compatible `artifact` identity in uploads and route inventories.

## Dry run

From this directory:

```powershell
python -m comma_companion_importer D:\comma_driving_logs --dry-run
```

Add `--list` for an exact tab-separated file list. A dry run does not create
the manifest and does not contact the server.

Pass `--inventory-config` during a dry run to preview whether every selected
route will be `complete` or `partial`, including its missing segment and
expected-stream counts.

Date filters are UTC and use an inclusive `--since` and exclusive `--until`.
If the route name contains `YYYY-MM-DD--HH-MM-SS`, that timestamp is used;
otherwise the file modification time is used.

Examples:

```powershell
# Only full-quality road/wide video and rlogs from selected routes
python -m comma_companion_importer D:\comma_driving_logs --dry-run `
  --cameras road,wide --logs rlog --no-other --route "0000011c*"

# Exclude video but retain all logs and metadata from July 2026
python -m comma_companion_importer D:\comma_driving_logs --dry-run `
  --cameras none --since 2026-07-01 --until 2026-08-01
```

## Upload

Use the dedicated importer token, not an administrator session or device
token. The importer credential is intentionally rejected at the public reverse
proxy. First open the host-only port through SSH:

```powershell
ssh -NT -L 18000:127.0.0.1:18000 USER@WEBSERVER
```

Then point the importer at that loopback tunnel:

```powershell
$env:COMMA_COMPANION_URL = "http://127.0.0.1:18000"
$env:COMMA_COMPANION_IMPORT_TOKEN = "<import token>"

python -m comma_companion_importer D:\comma_driving_logs `
  --device-id "<enrolled Comma Companion device ID>" `
  --inventory-config .\inventory.json `
  --manifest "$env:LOCALAPPDATA\CommaCompanion\historical-import.sqlite3" `
  --concurrency 2 --bandwidth-mbps 40
```

Plain HTTP is accepted only for a loopback origin; SSH supplies transport
encryption. The server separately verifies the exact Docker bridge peer created
by its host-only port, so a copied importer bearer still cannot be used through
`https://comma.danielv.no`.

Use the same `--device-id` as the live agent. Without an override, the scanner
infers the directory immediately above `realdata`; in the current copied
archive that is `10.30.1.75`, which is only correct if the server enrolled that
exact ID.

## Immutable route inventory

Every route upload requires an explicit `--inventory-config`. The JSON uses the
same `inventory.expected_streams` schema as the device agent:

```json
{
  "inventory": {
    "expected_streams": [
      {
        "root_name": "realdata",
        "artifact_type": "rlog",
        "camera": ""
      },
      {
        "root_name": "realdata",
        "artifact_type": "video",
        "camera": "road"
      }
    ]
  }
}
```

A complete agent configuration can also be supplied; the importer reads its
`inventory` object and ignores unrelated top-level agent settings. Stream
types are `video`, `rlog`, and `qlog`. Video requires a camera; logs use an
empty or omitted camera.

Upload selection and expected capability are deliberately separate.
`--cameras road --logs rlog` decides which files are transferred during this
run. Once a route is selected, its immutable inventory is built from one
unfiltered scan of that route under the source root. Camera, log, artifact,
date, and size selection cannot hide a higher segment or observed role and
falsely close a truncated route. Filtered-away files remain declared and keep
the drive processing until a later import archives them.

The inventory config states which streams must exist in every segment. For
each root actually used by a route, the importer unions that root's explicitly
configured roles with repeatable streams observed in the authoritative
snapshot. Configured alternative roots that are not active on the route are
ignored. Every active root needs its own explicit profile including an rlog
role before the route can be complete. The `realdata`, `realdata_HD`, and
`realdata_konik` namespaces are alternative authoritative log roots; if more
than one is active for the same route, the snapshot is explicitly partial with
`multiple_active_log_roots` evidence rather than choosing one silently.

Non-stream metadata, stats, crash files, and other selected files are included
in the immutable file inventory. They use the device agent's canonical
`artifact` kind in both the upload declaration and inventory, so the server can
match the archived object exactly.

The source passed to one invocation is an authoritative, closed historical
snapshot. The importer records `route_closed=true` with
`historical_static_snapshot` and `source_scan_complete` evidence. It enumerates
every segment from zero through the highest observed number and records gaps
and absent expected streams explicitly. A route becomes:

- `complete` only when at least one rlog is expected, all segment numbers are
  present, and every expected stream exists in every segment;
- `partial` when a segment or stream is missing.

Do not point the importer at one segment directory and treat it as a whole
route unless that partial static snapshot is intentional. To import a selected
set such as segments 98 and 99, stage both below one temporary archive root and
run one scan. This lets the manifest record both present segments and the
missing range 0 through 97 atomically.

Every stream file is SHA-256 hashed and re-statted before the inventory is
declared. The local SQLite database stores the exact canonical manifest,
generation, predecessor digest, and server acceptance. A crash or retry
replays the byte-identical digest and frozen `closed_at` timestamp rather than
creating a new generation. An unchanged server inventory is reused.

If the server already has a different inventory head that this local manifest
does not recognize, the importer refuses to overwrite the latest drive view.
Review the current server manifest, then explicitly provide an exact
force-with-lease value:

```powershell
python -m comma_companion_importer D:\staged-route `
  --inventory-config .\inventory.json `
  --supersede-inventory "ROUTE_NAME=LATEST_MANIFEST_SHA256"
```

The operation fails if the server head changed after it was reviewed.

The SQLite manifest is local to the workstation by default. It stores file
size, modification time, lazily computed SHA-256, server upload ID,
server-confirmed offset, and immutable route inventory generations. Entries
are scoped to the normalized server URL.
The CLI rejects a manifest path inside the source archive.
Re-running the same command checks completed uploads with `HEAD` and resumes at
the durable server offset. If a server was restored without its catalog, the
file is declared again instead of being silently skipped. A changed source
file identity or declaration is reset on the next scan; it is never modified by
the importer. Use `--rehash-completed` when sources may have been overwritten
in place while retaining identical size and timestamps; this performs a full
read of every completed source.

Before hashing a device's route snapshot, the importer processes its smallest
selected file as an authentication/enrollment preflight. If that fails, the
remaining files for that device are left unread so a wrong `--device-id`
cannot trigger a full-archive hashing pass. After that preflight, the route
streams are hashed, the inventory is durably recorded and declared, and the
remaining files are uploaded.

Logical paths retain their comma log-root namespace (`realdata`,
`realdata_HD`, or `realdata_konik`) so a historical import and the live agent
refer to the same artifact rather than creating duplicate catalog entries.

Uploads use 16 MiB chunks by default and the CLI caps custom chunks at the
server's 16 MiB maximum. `POST /api/v1/uploads` declares
`route_name`, `segment_number`, `artifact_type`, `camera`, and
`relative_path`; `HEAD` reconciles the durable offset; `PATCH` sends binary
chunks. Retries use exponential backoff. `--bandwidth-mbps` is one shared
decimal-Mbit/s cap across all `--concurrency` workers. Progress reports exact
source bytes, server-confirmed durable bytes, and bytes attempted in this run.

Useful selection switches:

- `--cameras road,wide,driver,qcamera|all|none`
- `--exclude-cameras ...`
- `--logs rlog,qlog|all|none`
- `--exclude-logs ...`
- repeatable `--route GLOB` and `--exclude-route GLOB`
- `--since`, `--until`, `--include-artifact`, `--exclude-artifact`
- `--no-other`
- `--inventory-config PATH`
- repeatable `--supersede-inventory ROUTE=SHA256`

## Single-file executable

Build a standard-library-only Python zipapp:

```powershell
python build_zipapp.py
python .\dist\comma-companion-import.pyz D:\comma_driving_logs --dry-run
```

The zipapp still requires Python 3.11 or newer, but no packages need to be
installed.

## Tests

```powershell
python -m pytest -q
```
