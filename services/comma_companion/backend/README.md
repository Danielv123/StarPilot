# Comma Companion backend

The backend is a standalone FastAPI service. Its SQLite database and session
state must use local VM storage; the archive may use a mounted SMB share.

## Secure bootstrap

Install the locked project and generate an Argon2id password hash without
placing the password in shell history:

```sh
uv sync --frozen --extra test
uv run comma-companion-hash-password
```

Configure the container with secrets supplied by the deployment platform:

```text
COMPANION_DATABASE_PATH=/var/lib/comma-companion/companion.sqlite3
COMPANION_SESSION_DIR=/var/lib/comma-companion/sessions
COMPANION_ARCHIVE_ROOT=/archive/comma-companion
COMPANION_PUBLIC_ORIGIN=https://comma.danielv.no
COMPANION_ALLOWED_ORIGINS=https://comma.danielv.no
COMPANION_ADMIN_USERNAME=admin
COMPANION_ADMIN_PASSWORD_HASH=<argon2id output>
COMPANION_SESSION_SECRET=<at least 32 random characters>
COMPANION_DEVICE_TOKENS_JSON={"<dongle-id>":"<random device token>"}
COMPANION_IMPORT_TOKEN=<independent random importer token>
```

Device tokens are hashed before storage. Tokens enrolled through
`POST /api/v1/devices` are returned once and never logged or shown again.
Configured tokens provide a noninteractive deployment path. The importer token
may declare uploads for any already-enrolled device; it is intentionally
separate from every device token.

The importer token is accepted only when Uvicorn's normalized client address
is in `COMPANION_IMPORT_ALLOWED_CLIENTS`, whose default is the exact loopback
addresses `127.0.0.1,::1`. Docker DNAT can present the exact bridge gateway
inside the container; deployment bootstrap must add that one address when it
is observed, never a subnet or wildcard. Use the host's loopback-published
port directly or through the documented SSH tunnel. Forwarding headers
supplied by an untrusted client do not satisfy this check, and the public
reverse-proxy path must not rewrite a remote client as an allowed local
address.

Device disablement is deliberately persistent: configured-token bootstrap may
rotate the stored token hash, but it never clears `devices.disabled_at`.
Disabled tokens cannot authenticate or update heartbeat state. The initial
release has no public disable or re-enable endpoint, so that lifecycle remains
an explicit database/operator action until an audited, step-up-protected
administrative workflow is added. Configured tokens are reconciled only at
process startup, and removing a device ID from
`COMPANION_DEVICE_TOKENS_JSON` does not disable or remove its existing database
record. Operators must set `disabled_at` explicitly to revoke that credential.

The session cookie is HttpOnly, Secure, SameSite=Strict, has no Domain
attribute, and defaults to a `__Host-` name. Keep TLS termination and the API on
the same public origin. The Uvicorn entry point trusts forwarded headers only
from `127.0.0.1` by default; set `FORWARDED_ALLOW_IPS` to the exact reverse-proxy
address when the proxy runs elsewhere.

Run the API with:

```sh
uv run comma-companion-api
```

Product APIs are under `/api/v1`; OpenAPI is available at
`/api/openapi.json`. Browser mutations require an allowed `Origin`, and durable
resource mutations require `Idempotency-Key`.

## Worker boundary

Upload finalization computes the full SHA-256, atomically installs the immutable
object, catalogs it, then queues jobs without running media or telemetry work in
the request process:

```text
verify_artifact      {"artifact_id":"..."}
transcode_video      {"artifact_id":"..."}
extract_telemetry    {"drive_id":"...","route_name":"..."}
simulate_counterfactual
```

Workers update job state/progress/error/result JSON. A validated AV1 WebM is
cataloged as a new `derived_video` artifact with the source device, drive,
segment and camera, a relative archive path, `codec=av1`, `mime_type=video/webm`,
duration in microseconds, `source_artifact_id`, and `status=ready`. Until that
row exists, drive playback returns `media_not_ready`; it never falls back to
video on the comma.
