# Comma Companion deployment

This directory builds one private Comma Companion image and runs two isolated
containers:

- `companion`, the FastAPI/Uvicorn process serving both `/api/v1` and the built
  Vite SPA;
- `worker`, exactly one durable SQLite job worker with no network interface or
  authentication-secret mount;
- one-shot media, rlog, and dynamics subprocess adapters launched by that
  worker;
- FFmpeg/ffprobe with `libsvtav1`, `libopus`, front-loaded WebM cues, and the
  reviewed CPU-only dynamics model.

The image does not contain the comma agent, historical log archive, Git
checkout, credentials, or any stock StarPilot upload modification. The server
integration stays under `services/comma_companion`.

## Published image

`.github/workflows/publish_comma_companion.yaml` builds the same reviewed
`linux/amd64` image on pushes that change Comma Companion release inputs. It
publishes branch and commit-SHA tags to:

```text
ghcr.io/danielv123/starpilot-comma-companion
```

The workflow verifies the tracked dynamics-model hash, computes the
deterministic source-bundle hash, records both hashes and the Git commit in OCI
metadata, generates SBOM and provenance attestations, and publishes only after
the Dockerfile's adapter/capability checks pass. Deploy by digest, never by a
mutable branch or `latest` tag:

```sh
docker pull ghcr.io/danielv123/starpilot-comma-companion@sha256:DIGEST
```

The GHCR package must be set to public after its first publication. Package
visibility is separate from repository visibility.

## Host layout

The deployment has three intentionally separate host paths:

```text
/var/lib/comma-companion/database/  local VM: SQLite, WAL, worker lock
/var/lib/comma-companion/sessions/  local VM: API sessions, invisible to worker
/archive/comma-companion/           SMB/CIFS: raw, uploads, derived output
./secrets/                           API secrets plus root-only transfer file
```

SQLite must remain on local storage. Do not place
`/var/lib/comma-companion` on CIFS, NFS, or another network filesystem. The
Compose bind mounts use `create_host_path: false`, and startup requires an
archive sentinel, so a missing SMB mount cannot silently redirect a large
upload to the VM's root disk.

Both containers run as UID/GID `65532`, have read-only root filesystems, no
Linux capabilities, no-new-privileges, bounded tmpfs/process limits, and no
additional swap allowance. The API is limited to 768 MiB RAM and one CPU; the
worker is limited to 2304 MiB RAM and two CPUs. The combined 3-GiB ceiling
leaves roughly 1 GiB on a 4-GB webserver for the OS and reverse proxy. Tini
forwards signals independently, so an FFmpeg or dynamics-worker failure cannot
take down the API.

The API alone mounts the secret directory, API session directory, and Nginx
Proxy Manager network. The worker sees the shared database directory but not
API session files. It has `network_mode: none` and fails startup if any
authentication secret or secret-file variable is supplied.

The worker's archive parent is read-only, including immutable
`objects/sha256` and in-progress `uploads`. Only nested `derived`, `telemetry`,
and `thumbnails` mounts are writable. Startup proves this boundary with actual
write-denial/write-success probes. Rlogs are copied into telemetry staging
after hash verification; no hardlink or writable symlink can alias a raw
object. FFmpeg receives raw inputs through the read-only view. The worker's one
nonblocking lock under its database runtime directory rejects a second durable
worker.

The native FFmpeg, capnp, and PyTorch parsers still run as children of the
worker UID and can write the shared queue database and the three output
directories. Their raw inputs are immutable, they receive no credentials or
network interface, and the container retains the same capability, process,
memory, and read-only-root restrictions. A future per-job sidecar or syscall
sandbox would further reduce the parser boundary; it is not a substitute for
the raw read-only mounts enforced here.

## 1. Mount the SMB archive

Install `cifs-utils`, create a root-only credentials file, and mount the share
on the host. A representative `/etc/fstab` entry is:

```fstab
//SMB-SERVER/SHARE /archive/comma-companion cifs credentials=/etc/samba/comma-companion.credentials,vers=3.1.1,seal,uid=65532,gid=65532,file_mode=0600,dir_mode=0700,nosuid,nodev,noexec,_netdev,nofail,x-systemd.automount 0 0
```

The credentials file contains:

```ini
username=comma-companion
password=REPLACE_ME
domain=WORKGROUP
```

Protect it with `root:root` and mode `0600`. Adjust the SMB path, domain, and
`seal` option to the server's actual capabilities. Do not put SMB credentials
in Compose, the image, or `.env`.

Mount and verify it before bootstrap:

```sh
sudo mkdir -p /archive/comma-companion
sudo mount /archive/comma-companion
findmnt -T /archive/comma-companion -o TARGET,SOURCE,FSTYPE,OPTIONS
```

The media worker publishes by same-directory `fsync` and `os.replace`.
Telemetry staging deliberately copies each selected rlog, so extraction needs
temporary free space up to the selected rlog size. Step 2 runs image-level
preflights against the real share and the worker's nested mount boundary.

## 2. Configure Compose and secrets

The existing Nginx Proxy Manager network must be named `webserver-proxy`, or
set `COMPANION_PROXY_NETWORK` to its actual name.

For the existing `UbuntuWeb` stack, merge
`deploy/ubuntuweb-compose.fragment.yml` into
`/home/danielv/docker/UbuntuWeb/docker-compose.yml` after replacing its
`IMAGE_DIGEST` placeholder. That fragment uses the host's existing external
`web` network, stores SQLite and sessions on the local system disk, stores
archive data under `/mnt/media/frog/comma-companion`, and exposes only
`192.168.10.37:18000`. It runs as the host's unprivileged UID/GID `1000`
because the already-mounted CIFS share forces that ownership.

Create the fragment's directories before Compose validation:

```sh
cd /home/danielv/docker/UbuntuWeb
install -d -m 0700 \
  comma-companion/state/database \
  comma-companion/state/sessions \
  comma-companion/secrets
install -d -m 0755 \
  /mnt/media/frog/comma-companion \
  /mnt/media/frog/comma-companion/objects/sha256 \
  /mnt/media/frog/comma-companion/uploads \
  /mnt/media/frog/comma-companion/derived \
  /mnt/media/frog/comma-companion/telemetry \
  /mnt/media/frog/comma-companion/thumbnails
touch /mnt/media/frog/comma-companion/.comma-companion-archive
```

Keep the HTTPS origin and secure cookie settings in the fragment even before
the proxy hostname is live. The unauthenticated health check is available on
the direct LAN port, but administrator login, device credentials, and imports
must wait for the HTTPS hostname rather than crossing plain HTTP.

```sh
cd services/comma_companion
cp .env.example .env
chmod 0600 .env
docker network inspect webserver-proxy
```

Review the working tree, then compute both provenance values immediately before
the build. `STARPILOT_COMMIT` records the base commit. The deterministic source
bundle SHA-256 covers the actual server source, deployment files, frontend,
locks, and copied cereal schemas, including intentional uncommitted files. It
normalizes directories to mode `0755`, shebang files to `0755`, ordinary files
to `0644`, and symlinks to `0777`, so Windows and Linux recompute the same hash
from the same bytes and file kinds. The image build tag uses that source hash,
not the base commit.

```sh
git -C ../.. status --short
export STARPILOT_COMMIT=$(git -C ../.. rev-parse HEAD)
export COMPANION_SOURCE_SHA256=$(python3 deploy/source_manifest.py)
export SOURCE_IMAGE="comma-companion:$COMPANION_SOURCE_SHA256"
sed -i "s/^STARPILOT_COMMIT=.*/STARPILOT_COMMIT=$STARPILOT_COMMIT/" .env
sed -i "s/^COMPANION_SOURCE_SHA256=.*/COMPANION_SOURCE_SHA256=$COMPANION_SOURCE_SHA256/" .env
sed -i "s|^COMMA_COMPANION_IMAGE=.*|COMMA_COMPANION_IMAGE=$SOURCE_IMAGE|" .env
mkdir -p runtime-manifests
python3 deploy/source_manifest.py --manifest > "runtime-manifests/source-$COMPANION_SOURCE_SHA256.manifest"
python3 deploy/source_manifest.py \
  --archive "runtime-manifests/source-$COMPANION_SOURCE_SHA256.tar"
python3 deploy/validate_compose.py --release
docker compose build --pull companion
python3 deploy/validate_compose.py --release --image
python3 deploy/pin_image.py "$SOURCE_IMAGE"
```

`pin_image.py` rechecks the current source bundle and atomically replaces the
source tag in mode-`0600` `.env` with the immutable local image config ID.
Everything started below uses `--no-build`; a mutable tag can therefore never
silently rebuild or move at deployment time. A local image ID is specific to
this Docker image content and must be preserved with `docker image save` for
host recovery; a pushed registry digest is the alternative for multi-host use.

For Windows-to-Linux transfer, copy the generated `.tar` without unpacking and
extract it into a new empty release root on the Linux host. The tar contains the
exact source-manifest entries, normalized metadata, empty directories, and real
symlinks. Transfer the separately hashed model artifact to its configured
`artifacts/tuning/...` path, then prove both hashes before building:

```sh
mkdir -m 0755 /srv/comma-companion-release
tar -xf "source-$COMPANION_SOURCE_SHA256.tar" \
  -C /srv/comma-companion-release
cd /srv/comma-companion-release
test "$(python3 services/comma_companion/deploy/source_manifest.py)" \
  = "$COMPANION_SOURCE_SHA256"
test "$(sha256sum artifacts/tuning/neural_lateral_plant_20260723/neural_lateral_plant.pt | awk '{print $1}')" \
  = "fb1b8b951fdff19ff5f9349470415d003b61ee5655fff996365b429d93f6dc45"
```

Do not copy a Windows-expanded tree with ad hoc permission repair. Keep the
source tar and its SHA-256 beside the release manifest, and build from the
verified extraction. The four `COMMA_DYNAMICS_MODEL_*` values form one reviewed
model release:
local build context, filename at that context root, fixed in-image path, and
expected SHA-256. `deploy/validate_compose.py` hashes the source artifact before
deployment, and the image build independently asks the dynamics adapter for
the loaded model hash. The defaults deliberately remain on the reviewed
promoted artifact configured in `.env.example`. Change them together only after
a replacement artifact has completed causal-data review and promotion.

The Node, Python, and uv base references include immutable multi-platform
manifest digests. Their exact references, base commit, source-bundle hash, and
model path/hash are OCI labels checked against rendered Compose after build.
Do not replace them with tag-only references.

Review `.env`; it contains only configuration and host paths. Sensitive values
are separate files so Argon2's `$` characters and tokens never pass through
Compose interpolation or image layers.

The explicit per-type artifact, active-upload, pending-byte, in-flight PATCH,
job-count, body-size, stale-upload, and archive-free-space limits are admission
controls, not sizing suggestions. Video, log, and other objects default to
1 GiB, 64 MiB, and 256 MiB respectively; those caps match downstream parser
limits. Keep the global limits at least as large as the per-device limits and
the per-device pending-byte limit at least as large as the global maximum
artifact setting. New uploads are refused when the archive would fall below
the larger of the 10-GiB or 5-percent reserve. The Compose validator rejects
missing, unbounded, or inconsistent values.

Bootstrap takes the device ID that the comma agent will use. It refuses a
non-CIFS archive, generates independent session/import/device tokens without
overwriting existing files, rejects broad/overlapping/symlinked host paths,
creates the SMB sentinel, and prompts for the administrator password:

```sh
sudo sh deploy/bootstrap.sh YOUR_COMMA_DEVICE_ID
```

For automated bootstrap, stage one plaintext administrator password under a
root-owned, non-writable path such as `/root`, then supply only its path:

```sh
sudo install -o root -g root -m 0400 \
  /secure-transfer/admin-password \
  /root/comma-companion-admin-password
sudo COMPANION_ADMIN_PASSWORD_FILE=/root/comma-companion-admin-password \
  sh deploy/bootstrap.sh YOUR_COMMA_DEVICE_ID
```

The input must be an absolute-path, root-owned regular non-symlink file with
mode `0400` or `0600`, one hard link, safe root-owned parent directories, and
exactly one UTF-8 line. Its bytes go directly to the Argon2 helper over stdin;
they are never printed, placed in an argument/environment variable, or copied
into `.env`. Bootstrap consumes and removes the plaintext file immediately
after the hash is safely installed. Without this option, the interactive
fallback requires the password twice.

The bootstrap reads the four deployment path/network values from `.env`;
explicit exported values take precedence when deliberately preserved through
`sudo`. It resolves the Docker proxy network's exact gateway and atomically
writes loopbacks plus that one address to
`COMPANION_IMPORT_ALLOWED_CLIENTS`. Docker presents a connection to the
host-published loopback port as this gateway inside the container.

Dedicated state and secrets roots are `root:65532` mode `0750`, with
`root:root` mode-`0400` ownership markers. Database/session subdirectories are
`65532:65532` mode `0700`; the four API secret files are `65532:65532` mode
`0400`. Bootstrap never prints a bearer token. It writes the server token map
for the API and a separate `root:root` mode-`0400` transfer file:

```text
secrets/session-secret
secrets/admin-password-hash
secrets/device-tokens.json
secrets/import-token
secrets/device-token-YOUR_COMMA_DEVICE_ID
```

Transfer the token file directly over SSH to the staged agent without reading
it into a terminal, environment, or command argument:

```sh
sudo scp -p "secrets/device-token-YOUR_COMMA_DEVICE_ID" \
  comma@COMMA_HOST:/data/private-pond-agent/device-token
```

Then configure the same device ID and run the agent's `install-device.sh`; its
installer moves the token into the root-only credential store. Never `cat` the
token, commit it, copy it into an image, or put its bytes in shell history. The
service `.gitignore` and `.dockerignore` exclude `.env`, secrets, token staging
variants, deployment snapshots/backups/image archives/runtime manifests, local
environments, `node_modules`, and generated output.

Now validate the pinned image labels and resolved importer gateway:

```sh
python3 deploy/validate_compose.py --release --image
```

Test the real SMB share from the final image:

```sh
docker compose run --rm --no-deps \
  --entrypoint python companion \
  /usr/local/lib/comma-companion/preflight_storage.py
docker compose run --rm --no-deps \
  --entrypoint python worker \
  /usr/local/lib/comma-companion/preflight_storage.py --worker-layout
```

The command fails unless `filesystem` is `cifs`/`smb3`, local state is writable
on a local non-network filesystem and a different device, SQLite exclusive
locking works, and archive `atomic_replace`, `file_fsync`, and `hardlink` are
true. The worker check independently proves raw/upload write denial,
output/database writability, hardlink support in every derived-output
directory, local database storage with SQLite locking, and absence of the API
session mount.

Validate and start:

```sh
docker compose config --quiet
python3 deploy/validate_compose.py --release
docker compose up -d --wait --no-build
docker compose ps
docker compose logs --tail=100 companion worker
```

The public JSON health endpoint is deliberately cheap process liveness. It
returns only `status`, `service`, and `time`; it does no SQLite or CIFS I/O:

```sh
curl --fail --silent http://127.0.0.1:18000/api/v1/health
docker inspect comma-companion --format '{{json .State.Health}}'
docker inspect comma-companion-worker --format '{{json .State.Health}}'
docker top comma-companion -eo pid,ppid,user,args
docker top comma-companion-worker -eo pid,ppid,user,args
```

`GET /api/v1/readiness` is the expensive authenticated operator check. It
returns `200` only when SQLite and the CIFS archive are ready, otherwise `503`.
Send it only over the host-loopback path with a normal administrator session;
never put the session cookie in shell history. The public NPM server hard-denies
this path as `404`.

The API process list should contain Tini and `comma-companion-api`. The worker
list should contain Tini and exactly one `comma-companion-worker`; adapter and
FFmpeg children appear there only while a job is active. Worker health is
entirely local: it verifies the singleton lock, database schema, read-only raw
view, writable output submounts, and sentinel without depending on the API or
network.

## 3. Nginx Proxy Manager

Attach Nginx Proxy Manager and only the `companion` API service to the same
external `webserver-proxy` network. The worker must remain network-disabled.
Do **not** create a `comma.danielv.no` Proxy Host in the NPM UI and do not edit
generated `proxy_host/*.conf` files. The managed standalone server owns this
hostname exactly once through NPM 2.15's persistent `http_top.conf` and
`http.conf` hooks. A UI host would create a conflicting second owner.

First pin the NPM container to a stable address on that network and put that
one exact address in `.env`:

```sh
NPM_IP=$(docker inspect \
  --format '{{with index .NetworkSettings.Networks "webserver-proxy"}}{{.IPAddress}}{{end}}' \
  nginx-proxy-manager)
test -n "$NPM_IP"
sed -i "s/^FORWARDED_ALLOW_IPS=.*/FORWARDED_ALLOW_IPS=$NPM_IP/" .env
docker compose up -d --wait --no-build
python3 deploy/validate_compose.py --release --image
```

Do not use a CIDR or `*`, and do not add the Docker gateway written to
`COMPANION_IMPORT_ALLOWED_CLIENTS`. The manager independently checks that the
running API's `FORWARDED_ALLOW_IPS` equals the NPM container's current network
address. If the NPM address changes, update `.env` and recreate only the API
before attempting an NPM action.

Stage the Cloudflare origin pair without displaying the key:

```sh
sudo install -d -o root -g root -m 0700 \
  /home/danielv/.local/share/comma-companion-origin
sudo install -o root -g root -m 0644 /secure-transfer/origin.pem \
  /home/danielv/.local/share/comma-companion-origin/origin.pem
sudo install -o root -g root -m 0400 /secure-transfer/origin.key \
  /home/danielv/.local/share/comma-companion-origin/origin.key
```

The certificate must cover `comma.danielv.no`, match the key, and remain valid
for at least seven days. Install the versioned release:

```sh
python3 deploy/npm/verify_config.py deploy/npm
sudo sh deploy/npm/manage.sh install
sudo sh deploy/npm/manage.sh check
```

The manager requires NPM `2.15.1+` within the reviewed 2.15 line and OpenResty
`1.29.2.5+`, takes a singleton root lock, snapshots and revalidates the origin
pair, copies an immutable release under
`/data/nginx/custom/comma-companion/releases`, preserves existing singleton
hooks, atomically switches `current`, runs `nginx -t`/`nginx -T` as NPM's real
UID/GID, and reloads. Any failure or signal after promotion restores the prior
symlink/hooks and reloads them. It prints the prior release ID for explicit
rollback; `sudo sh deploy/npm/manage.sh rollback RELEASE_ID` revalidates that
exact policy and certificate before activation.

The installed server:

- accepts origin connections only from Cloudflare's complete current IPv4/IPv6
  ranges and requires `CF-Connecting-IP`;
- restores the visitor IP, then clears client-supplied `Forwarded` and
  `CF-Connecting-IP` before proxying;
- uses Docker DNS `127.0.0.11` with a ten-second validity so API container
  replacement does not pin a stale address;
- keeps ordinary JSON requests buffered at 2 MiB with 30-second timeouts;
- grants the exact 32-lowercase-hex upload item path a 20-MiB body ceiling,
  ten-minute inactivity timeouts, four-connection limit, and unbuffered request
  streaming for GET/HEAD/PATCH only;
- rate-limits health, login, enrollment/admin mutations, and sustained public
  route-inventory POSTs (`1r/s`, burst 10), while GET inventory remains on the
  normal API path;
- exposes bounded cheap health, returns `404` for public readiness, and disables
  response temp-file spill so concurrent media streams cannot fill NPM's
  writable layer.

The verifier structurally binds every location, gate, normalized header,
timeout, body ceiling, method restriction, resolver, and current Cloudflare
range. Because the reviewed OpenResty build predates later fixes, `nginx -T`
also rejects risky constructs anywhere in the shared effective config: regex
map keys, `slice`, `ssi on`, upstream HTTP/2, `grpc_pass`, and
`source_charset`. Reassess this gate only with a reviewed patched NPM image.

After the origin config passes, enable the Cloudflare proxy and **Full
(strict)**. Flexible mode is forbidden. The template intentionally contains no
HSTS; the verifier rejects premature HSTS. Add it only after strict origin
reachability and certificate renewal are proven, initially without
`includeSubDomains` or preload. Also restrict the host firewall to Cloudflare's
published ranges so rejected direct traffic does not consume NPM resources.

Run the live boundary smoke:

```sh
sudo sh deploy/npm/smoke.sh
```

It proves a direct-origin request with a forged Cloudflare header receives
`403`, the real Cloudflare visitor IP reaches the backend, public readiness is
hidden, and only the upload item path receives the larger/longer body policy.
Then explicitly prove Docker DNS re-resolution:

```sh
docker compose up -d --wait --no-build --force-recreate companion
sudo sh deploy/npm/manage.sh check
sudo sh deploy/npm/smoke.sh
```

The only host-published application port is configurable
`127.0.0.1:${COMPANION_LOOPBACK_PORT:-18000}`. It is never bound to a public
interface. NPM uses the Docker network, not this port.

## Historical import without Cloudflare

For a large local archive, tunnel the loopback port over SSH so the data goes
directly to the webserver instead of through Cloudflare:

```sh
ssh -NT -L 18000:127.0.0.1:18000 USER@WEBSERVER
```

On the workstation, use the tunnel origin and the dedicated importer token:

```powershell
Set-Location C:\path\to\StarPilot\services\comma_companion\tools\importer

$env:COMMA_COMPANION_URL = "http://127.0.0.1:18000"
$tokenPath = "$env:USERPROFILE\.comma-companion\import-token"
$env:COMMA_COMPANION_IMPORT_TOKEN = [IO.File]::ReadAllText($tokenPath).Trim()

python -m comma_companion_importer D:\comma_driving_logs `
  --device-id "<same enrolled device ID>" `
  --manifest "$env:LOCALAPPDATA\CommaCompanion\historical-import.sqlite3" `
  --concurrency 2 --bandwidth-mbps 40

Remove-Item Env:COMMA_COMPANION_IMPORT_TOKEN
```

Transfer `secrets/import-token` to that user-only workstation file without
printing it, and restrict its ACL before loading it. The importer accepts plain
HTTP only for loopback origins. The SSH tunnel provides transport encryption;
Docker DNAT then presents the exact bridge gateway bootstrap allowed. The
public reverse proxy cannot use the importer credential, even if the bearer
token is otherwise correct. Uvicorn must trust only the exact proxy address,
and NPM must replace client-supplied forwarding headers as described above.

After NPM and both containers are healthy, prove both sides of this boundary
before transferring the importer credential:

```sh
sudo python3 deploy/smoke_importer_auth.py \
  --url http://127.0.0.1:18000 \
  --token-file secrets/import-token \
  --expect allowed
PROXY_GATEWAY=$(docker network inspect \
  --format '{{(index .IPAM.Config 0).Gateway}}' webserver-proxy)
sudo python3 deploy/smoke_importer_auth.py \
  --url https://comma.danielv.no \
  --token-file secrets/import-token \
  --expect rejected \
  --spoof-forwarded-for "${PROXY_GATEWAY}"
```

The allowed probe reaches authentication and stops at a deliberately
nonexistent device, so it stores no upload. The public probe forges the trusted
gateway header and must still receive `importer_source_not_allowed`; this also
checks NPM header replacement. Re-running the real importer resumes from
server-confirmed offsets.

## Backup

A complete application-data restore requires all three durable layers:

1. local `/var/lib/comma-companion/database` for SQLite, WAL, and worker lock,
   plus `/var/lib/comma-companion/sessions` for API sessions;
2. the complete SMB `/archive/comma-companion` tree;
3. the four API secret files, stored in a separate encrypted backup. Preserve
   the root-only device transfer file too if reinstalling the agent must remain
   possible without rotating its credential.

Deployment recovery additionally requires the mode-`0600` `.env`, the exact
image, source archive/manifest, installed NPM custom hooks/releases, and origin
TLS material. A local Docker config ID written in `.env` is not recoverable on
another host merely by recording its text; save the image itself or publish it
under a registry digest.

The safest coordinated backup is:

1. `docker compose stop -t 120`;
2. take an SMB/NAS snapshot;
3. copy the local state directory while stopped, preserving numeric ownership;
4. encrypt and back up secrets separately;
5. save the exact image and record its OCI labels:

   ```sh
   DEPLOYED_IMAGE=$(docker compose images -q companion)
   docker image save --output runtime-manifests/deployed-image.tar \
     "$DEPLOYED_IMAGE"
   docker image inspect "$DEPLOYED_IMAGE" \
     > runtime-manifests/deployed-image.json
   ```

6. preserve `.env`, `docker compose config`, the source tar/manifest/hash,
   pinned base references, and snapshot IDs;
7. back up NPM's `/data/nginx/custom/http_top.conf`,
   `/data/nginx/custom/http.conf`, and
   `/data/nginx/custom/comma-companion` together, with the staged origin pair
   encrypted separately;
8. `docker compose start` and wait for both health checks.

Do not treat Git or the image as a data rollback. Raw objects and derived AV1
live only on the archive, while catalog relationships and jobs live in SQLite.

For an online SQLite-only checkpoint, use Python's backup API:

```sh
docker compose exec -T companion python -c \
  'import sqlite3; source=sqlite3.connect("/var/lib/comma-companion/database/companion.sqlite3"); target=sqlite3.connect("/var/lib/comma-companion/database/companion.backup.sqlite3"); source.backup(target); target.close(); source.close()'
```

That creates a consistent database copy, but it is not a coordinated archive
backup and therefore is insufficient by itself.

## Restore

Restore into new staging paths first; do not overwrite the only current copy.

1. Stop both containers and snapshot/copy the current state for rollback.
2. Load the saved image (or pull the recorded registry digest), then restore
   the matching `.env`.
3. Restore the matching SMB snapshot and local-state backup.
4. Restore the state/secrets roots and ownership markers as `root:65532` mode
   `0750` and `root:root` mode `0400`. Restore database/session directories as
   `65532:65532` mode `0700`, the four API files as `65532:65532` mode `0400`,
   and any device transfer file as `root:root` mode `0400`. Do not apply one
   blanket owner recursively.
5. Verify the SMB mount and sentinel.
6. Point `COMPANION_STATE_PATH` and `COMPANION_ARCHIVE_PATH` at the staged
   restore.
7. Run the two storage preflights and an offline SQLite check:

   ```sh
   docker compose run --rm --no-deps --entrypoint python companion \
     /usr/local/lib/comma-companion/preflight_storage.py
   docker compose run --rm --no-deps --entrypoint python worker \
     /usr/local/lib/comma-companion/preflight_storage.py --worker-layout
   docker compose run --rm --no-deps --entrypoint python companion -c \
     'import sqlite3; c=sqlite3.connect("/var/lib/comma-companion/database/companion.sqlite3"); print(c.execute("PRAGMA quick_check").fetchone()[0]); c.close()'
   ```

8. Run `python3 deploy/validate_compose.py --release --image`, start with
   `docker compose up -d --wait --no-build`, then verify login, one historical
   media stream, one telemetry route, and the queue dashboard before retiring
   the old paths.

The backend currently enforces an exact schema version. A database from a newer
release may require the matching image; rolling the image back alone is not a
safe schema rollback.

## Upgrade and rollback

Before every upgrade:

```sh
mkdir -p runtime-manifests
docker compose config > runtime-manifests/deployed-compose.yml
DEPLOYED_IMAGE=$(docker compose images -q companion)
docker image inspect "$DEPLOYED_IMAGE" \
  > runtime-manifests/deployed-image.json
docker image inspect "$DEPLOYED_IMAGE" \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
  > runtime-manifests/deployed-base-commit.txt
docker image save --output runtime-manifests/deployed-image.tar \
  "$DEPLOYED_IMAGE"
```

Take the coordinated backup above, then build the intended commit and validate
before replacing the containers:

```sh
export STARPILOT_COMMIT=$(git -C ../.. rev-parse HEAD)
export COMPANION_SOURCE_SHA256=$(python3 deploy/source_manifest.py)
export SOURCE_IMAGE="comma-companion:$COMPANION_SOURCE_SHA256"
sed -i "s/^STARPILOT_COMMIT=.*/STARPILOT_COMMIT=$STARPILOT_COMMIT/" .env
sed -i "s/^COMPANION_SOURCE_SHA256=.*/COMPANION_SOURCE_SHA256=$COMPANION_SOURCE_SHA256/" .env
sed -i "s|^COMMA_COMPANION_IMAGE=.*|COMMA_COMPANION_IMAGE=$SOURCE_IMAGE|" .env
python3 deploy/source_manifest.py --manifest \
  > "runtime-manifests/source-$COMPANION_SOURCE_SHA256.manifest"
python3 deploy/source_manifest.py \
  --archive "runtime-manifests/source-$COMPANION_SOURCE_SHA256.tar"
python3 deploy/validate_compose.py --release
docker compose build --pull companion
python3 deploy/validate_compose.py --release --image
python3 deploy/pin_image.py "$SOURCE_IMAGE"
sudo sh deploy/bootstrap.sh YOUR_COMMA_DEVICE_ID
python3 deploy/validate_compose.py --release --image
docker compose up -d --wait --no-build
```

Check both container health states, the singleton worker, worker network
isolation and absence of secret mounts/environment, FFmpeg capabilities, the
model hash, a small upload, AV1 playback with seeking, and telemetry/dynamics
replay. Keep the previous image and backup until those checks pass.

Rollback means loading/reactivating the recorded immutable image, restoring its
recorded `.env`, and, when the schema changed, restoring the compatible local
state plus coordinated SMB snapshot. Start it with `--no-build`. Do not delete
or rewrite archive objects during an image-only rollback. NPM policy rollback
is separate and uses `sudo sh deploy/npm/manage.sh rollback RELEASE_ID`.

## Useful diagnostics

```sh
docker compose logs --follow companion worker
docker stats comma-companion comma-companion-worker
docker inspect comma-companion \
  --format 'user={{.Config.User}} readonly={{.HostConfig.ReadonlyRootfs}} cpu={{.HostConfig.NanoCpus}} memory={{.HostConfig.Memory}} swap={{.HostConfig.MemorySwap}} pids={{.HostConfig.PidsLimit}} caps={{json .HostConfig.CapDrop}} security={{json .HostConfig.SecurityOpt}}'
docker inspect comma-companion-worker \
  --format 'user={{.Config.User}} network={{.HostConfig.NetworkMode}} readonly={{.HostConfig.ReadonlyRootfs}} cpu={{.HostConfig.NanoCpus}} memory={{.HostConfig.Memory}} swap={{.HostConfig.MemorySwap}} pids={{.HostConfig.PidsLimit}} caps={{json .HostConfig.CapDrop}} security={{json .HostConfig.SecurityOpt}} mounts={{json .Mounts}}'

docker compose run --rm --no-deps --entrypoint sh companion -ec '
  id
  ffmpeg -hide_banner -encoders 2>&1 | grep -E "libsvtav1|libopus"
  ffmpeg -hide_banner -h muxer=webm 2>&1 | grep -E "cues_to_front|reserve_index_space"
  comma-companion-media --help >/dev/null
  comma-companion-rlog --help >/dev/null
  printf "%s\n" "{\"id\":\"check\",\"method\":\"model_info\",\"params\":{}}" |
    comma-companion-dynamics
'
```

The build itself runs these adapter/capability checks and rejects a dynamics
artifact whose SHA-256 differs from the configured
`COMMA_DYNAMICS_MODEL_SHA256`. `deploy/validate_compose.py --release --image`
also verifies the image's source, base-image, commit, and model labels before
deployment.
