#!/bin/sh
set -eu

APP_UID=65532
APP_GID=65532
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SERVICE_ROOT=$(CDPATH= cd -- "${SCRIPT_DIR}/.." && pwd)
DOTENV_PATH=${SERVICE_ROOT}/.env

dotenv_value() {
  key=$1
  if [ ! -f "${DOTENV_PATH}" ]; then
    return
  fi
  awk -v key="${key}" '
    index($0, key "=") == 1 {
      sub("^[^=]*=", "")
      sub("\r$", "")
      value = $0
    }
    END { if (value != "") print value }
  ' "${DOTENV_PATH}"
}

set_dotenv_value() {
  key=$1
  value=$2
  temporary_path=$(mktemp --tmpdir="${SERVICE_ROOT}" ".env.new.XXXXXXXXXX")
  if ! awk -v key="${key}" -v value="${value}" '
    BEGIN { found = 0 }
    {
      sub("\r$", "")
      if (index($0, key "=") == 1) {
        print key "=" value
        found = 1
      } else {
        print
      }
    }
    END {
      if (!found) print key "=" value
    }
  ' "${DOTENV_PATH}" >"${temporary_path}"; then
    rm -f "${temporary_path}"
    printf 'Failed to update %s in %s.\n' "${key}" "${DOTENV_PATH}" >&2
    exit 1
  fi
  chown --reference="${DOTENV_PATH}" "${temporary_path}"
  chmod --reference="${DOTENV_PATH}" "${temporary_path}"
  mv "${temporary_path}" "${DOTENV_PATH}"
}

STATE_VALUE=${COMPANION_STATE_PATH:-$(dotenv_value COMPANION_STATE_PATH)}
ARCHIVE_VALUE=${COMPANION_ARCHIVE_PATH:-$(dotenv_value COMPANION_ARCHIVE_PATH)}
SECRETS_VALUE=${COMPANION_SECRETS_PATH:-$(dotenv_value COMPANION_SECRETS_PATH)}
PROXY_NETWORK=${COMPANION_PROXY_NETWORK:-$(dotenv_value COMPANION_PROXY_NETWORK)}
ADMIN_PASSWORD_FILE_VALUE=${COMPANION_ADMIN_PASSWORD_FILE:-}
STATE_VALUE=${STATE_VALUE:-/var/lib/comma-companion}
ARCHIVE_VALUE=${ARCHIVE_VALUE:-/archive/comma-companion}
SECRETS_VALUE=${SECRETS_VALUE:-./secrets}
PROXY_NETWORK=${PROXY_NETWORK:-webserver-proxy}

reject_ambiguous_dotenv_value() {
  label=$1
  value=$2
  case "${value}" in
    \"*|*\"|\'*|*\'|*" #"*|["	 "]*|*["	 "])
      printf '%s must be an unquoted value without inline comments or surrounding whitespace.\n' \
        "${label}" >&2
      exit 1
      ;;
  esac
}

reject_ambiguous_dotenv_value COMPANION_STATE_PATH "${STATE_VALUE}"
reject_ambiguous_dotenv_value COMPANION_ARCHIVE_PATH "${ARCHIVE_VALUE}"
reject_ambiguous_dotenv_value COMPANION_SECRETS_PATH "${SECRETS_VALUE}"
reject_ambiguous_dotenv_value COMPANION_PROXY_NETWORK "${PROXY_NETWORK}"

case "${STATE_VALUE}" in
  /*) STATE_PATH=${STATE_VALUE} ;;
  *) STATE_PATH=${SERVICE_ROOT}/${STATE_VALUE#./} ;;
esac
case "${ARCHIVE_VALUE}" in
  /*) ARCHIVE_PATH=${ARCHIVE_VALUE} ;;
  *) ARCHIVE_PATH=${SERVICE_ROOT}/${ARCHIVE_VALUE#./} ;;
esac
case "${SECRETS_VALUE}" in
  /*) SECRETS_PATH=${SECRETS_VALUE} ;;
  *) SECRETS_PATH=${SERVICE_ROOT}/${SECRETS_VALUE#./} ;;
esac

canonical_path_without_symlinks() {
  label=$1
  candidate=$2
  lexical=$(realpath --canonicalize-missing --no-symlinks -- "${candidate}")
  resolved=$(realpath --canonicalize-missing -- "${candidate}")
  if [ "${lexical}" != "${resolved}" ]; then
    printf '%s contains a symlink component; refusing bind-mount ambiguity: %s\n' \
      "${label}" "${candidate}" >&2
    exit 1
  fi
  printf '%s\n' "${resolved}"
}

reject_broad_path() {
  label=$1
  candidate=$2
  case "${candidate}" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|/var/lib)
      printf '%s is an unsafe broad system path: %s\n' "${label}" "${candidate}" >&2
      exit 1
      ;;
  esac
}

paths_overlap() {
  first=$1
  second=$2
  case "${first}/" in
    "${second}/"*) return 0 ;;
  esac
  case "${second}/" in
    "${first}/"*) return 0 ;;
  esac
  return 1
}

prepare_dedicated_root() {
  label=$1
  path=$2
  marker_name=$3
  marker_path=${path}/${marker_name}
  if [ -e "${path}" ]; then
    if [ -L "${path}" ] || [ ! -d "${path}" ]; then
      printf '%s must be a non-symlink directory: %s\n' "${label}" "${path}" >&2
      exit 1
    fi
    if [ -L "${marker_path}" ] || [ ! -f "${marker_path}" ]; then
      printf '%s already exists without its ownership marker; refusing it: %s\n' \
        "${label}" "${path}" >&2
      exit 1
    fi
    if [ "$(stat -c '%u:%g:%a' "${marker_path}")" != "0:0:400" ]; then
      printf '%s ownership marker has unsafe metadata: %s\n' "${label}" "${marker_path}" >&2
      exit 1
    fi
  else
    parent_path=$(dirname -- "${path}")
    if [ -L "${parent_path}" ] || [ ! -d "${parent_path}" ]; then
      printf '%s parent must already be a non-symlink directory: %s\n' \
        "${label}" "${parent_path}" >&2
      exit 1
    fi
    install -d -m 0750 -o root -g "${APP_GID}" "${path}"
    marker_temporary=$(mktemp --tmpdir="${path}" ".marker.new.XXXXXXXXXX")
    printf '%s\n' "Comma Companion dedicated ${label}" >"${marker_temporary}"
    chown root:root "${marker_temporary}"
    chmod 0400 "${marker_temporary}"
    mv "${marker_temporary}" "${marker_path}"
  fi
  chown root:"${APP_GID}" "${path}"
  chmod 0750 "${path}"
}

if [ "$(id -u)" -ne 0 ]; then
  printf '%s\n' "Run this bootstrap with sudo; it creates UID 65532-owned state and secret files." >&2
  exit 1
fi
if [ ! -f "${DOTENV_PATH}" ]; then
  printf '%s\n' "Copy .env.example to .env and review it before bootstrap." >&2
  exit 1
fi
if [ -L "${DOTENV_PATH}" ] || [ "$(stat -c %a "${DOTENV_PATH}")" != "600" ]; then
  printf '%s\n' ".env must be a regular non-symlink file with mode 0600." >&2
  exit 1
fi
if [ "$#" -ne 1 ] || [ -z "$1" ]; then
  printf '%s\n' "Usage: sudo deploy/bootstrap.sh <comma-device-id>" >&2
  exit 2
fi
DEVICE_ID=$1
case "${DEVICE_ID}" in
  *[!A-Za-z0-9._-]*)
    printf '%s\n' "Device ID may contain only letters, digits, dot, underscore, and hyphen." >&2
    exit 2
    ;;
esac
if ! command -v openssl >/dev/null 2>&1; then
  printf '%s\n' "openssl is required to generate deployment secrets." >&2
  exit 1
fi
if ! command -v findmnt >/dev/null 2>&1; then
  printf '%s\n' "findmnt is required to verify the SMB archive mount." >&2
  exit 1
fi
if ! command -v realpath >/dev/null 2>&1; then
  printf '%s\n' "realpath is required to validate deployment host paths." >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  printf '%s\n' "python3 is required to validate deployment secrets." >&2
  exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' "docker is required to inspect the proxy network and hash the administrator password." >&2
  exit 1
fi

ADMIN_PASSWORD_FILE_PATH=
if [ -n "${ADMIN_PASSWORD_FILE_VALUE}" ]; then
  case "${ADMIN_PASSWORD_FILE_VALUE}" in
    /*) ;;
    *)
      printf '%s\n' "COMPANION_ADMIN_PASSWORD_FILE must be an absolute path." >&2
      exit 1
      ;;
  esac
  ADMIN_PASSWORD_FILE_PATH=$(
    canonical_path_without_symlinks \
      "Administrator password file" \
      "${ADMIN_PASSWORD_FILE_VALUE}"
  )
  python3 - "${ADMIN_PASSWORD_FILE_PATH}" <<'PY'
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
try:
  metadata = path.lstat()
except OSError as error:
  raise SystemExit(f"cannot inspect administrator password file: {error}")
if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
  raise SystemExit(
    "administrator password file must be a regular non-symlink file",
  )
actual = (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode))
if actual not in {(0, 0, 0o400), (0, 0, 0o600)}:
  raise SystemExit(
    "administrator password file must be root:root mode 0400 or 0600",
  )
if metadata.st_nlink != 1:
  raise SystemExit("administrator password file must have exactly one hard link")
if metadata.st_size > 4097:
  raise SystemExit("administrator password file is unexpectedly large")
for parent in path.parents:
  parent_metadata = parent.stat()
  if parent_metadata.st_uid != 0 or parent_metadata.st_mode & 0o022:
    raise SystemExit(
      f"administrator password path has an unsafe parent directory: {parent}",
    )
data = path.read_bytes()
if data.endswith(b"\n"):
  data = data[:-1]
  if data.endswith(b"\r"):
    data = data[:-1]
if b"\r" in data or b"\n" in data:
  raise SystemExit("administrator password file must contain exactly one line")
try:
  password = data.decode("utf-8")
except UnicodeDecodeError as error:
  raise SystemExit(
    f"administrator password file is not valid UTF-8: {error}",
  ) from error
if not 1 <= len(password) <= 1024:
  raise SystemExit(
    "administrator password must contain between 1 and 1024 characters",
  )
PY
fi

COMPOSE_VERSION=$(docker compose version --short 2>/dev/null || true)
if ! python3 - "${COMPOSE_VERSION}" <<'PY'
import re
import sys

match = re.search(r"(\d+)\.(\d+)", sys.argv[1])
if match is None or tuple(map(int, match.groups())) < (2, 20):
  raise SystemExit(1)
PY
then
  printf '%s\n' "Docker Compose 2.20 or newer is required for named contexts and --wait." >&2
  exit 1
fi
if ! docker buildx version >/dev/null 2>&1; then
  printf '%s\n' "Docker Buildx/BuildKit is required for named build contexts." >&2
  exit 1
fi
if ! (
  cd "${SERVICE_ROOT}"
  docker compose config --format json >/dev/null
); then
  printf '%s\n' "Docker Compose cannot render this deployment configuration." >&2
  exit 1
fi
if ! docker network inspect "${PROXY_NETWORK}" >/dev/null 2>&1; then
  printf '%s\n' "Create or start the ${PROXY_NETWORK} Docker network before bootstrap." >&2
  exit 1
fi
PROXY_GATEWAY=$(
  docker network inspect \
    --format '{{(index .IPAM.Config 0).Gateway}}' \
    "${PROXY_NETWORK}"
)
case "${PROXY_GATEWAY}" in
  ""|*[!0-9A-Fa-f:.]*)
    printf '%s\n' "The proxy network did not report one exact IP gateway." >&2
    exit 1
    ;;
esac
IMPORT_ALLOWED_CLIENTS="127.0.0.1,::1,${PROXY_GATEWAY}"
set_dotenv_value COMPANION_IMPORT_ALLOWED_CLIENTS "${IMPORT_ALLOWED_CLIENTS}"

STATE_PATH=$(canonical_path_without_symlinks "Local state path" "${STATE_PATH}")
ARCHIVE_PATH=$(canonical_path_without_symlinks "Archive path" "${ARCHIVE_PATH}")
SECRETS_PATH=$(canonical_path_without_symlinks "Secrets path" "${SECRETS_PATH}")
reject_broad_path "Local state path" "${STATE_PATH}"
reject_broad_path "Archive path" "${ARCHIVE_PATH}"
reject_broad_path "Secrets path" "${SECRETS_PATH}"
if paths_overlap "${STATE_PATH}" "${ARCHIVE_PATH}" \
  || paths_overlap "${STATE_PATH}" "${SECRETS_PATH}" \
  || paths_overlap "${ARCHIVE_PATH}" "${SECRETS_PATH}"; then
  printf '%s\n' "Local state, archive, and secrets paths must not overlap." >&2
  exit 1
fi
if paths_overlap "${STATE_PATH}" "${SERVICE_ROOT}" \
  || paths_overlap "${ARCHIVE_PATH}" "${SERVICE_ROOT}"; then
  printf '%s\n' "Local state and archive paths must not overlap the source checkout." >&2
  exit 1
fi
case "${SERVICE_ROOT}/" in
  "${SECRETS_PATH}/"*)
    printf '%s\n' "Secrets path must not contain or equal the source checkout." >&2
    exit 1
    ;;
esac

if [ ! -d "${ARCHIVE_PATH}" ]; then
  printf '%s\n' "Archive path does not exist: ${ARCHIVE_PATH}" >&2
  exit 1
fi
ARCHIVE_FSTYPE=$(findmnt -T "${ARCHIVE_PATH}" -n -o FSTYPE || true)
if [ "${ARCHIVE_FSTYPE}" != "cifs" ]; then
  printf '%s\n' "Archive path is not on CIFS/SMB (found ${ARCHIVE_FSTYPE:-no mount}); refusing local fallback." >&2
  exit 1
fi
if [ ! -w "${ARCHIVE_PATH}" ]; then
  printf '%s\n' "Archive path is not writable by the bootstrap user: ${ARCHIVE_PATH}" >&2
  exit 1
fi
prepare_dedicated_root "state root" "${STATE_PATH}" ".comma-companion-state"
install -d -m 0700 -o "${APP_UID}" -g "${APP_GID}" \
  "${STATE_PATH}/database" \
  "${STATE_PATH}/sessions"
for archive_directory in \
  "${ARCHIVE_PATH}/objects" \
  "${ARCHIVE_PATH}/objects/sha256" \
  "${ARCHIVE_PATH}/uploads" \
  "${ARCHIVE_PATH}/derived" \
  "${ARCHIVE_PATH}/telemetry" \
  "${ARCHIVE_PATH}/thumbnails"
do
  mkdir -p "${archive_directory}"
  chown "${APP_UID}:${APP_GID}" "${archive_directory}" 2>/dev/null || true
  chmod 0700 "${archive_directory}" 2>/dev/null || true
done

prepare_dedicated_root "secrets root" "${SECRETS_PATH}" ".comma-companion-secrets"
umask 077

make_random_secret() {
  destination=$1
  bytes=$2
  if [ -e "${destination}" ]; then
    return
  fi
  temporary=$(mktemp --tmpdir="${SECRETS_PATH}" ".secret.new.XXXXXXXXXX")
  if ! openssl rand -hex "${bytes}" >"${temporary}"; then
    rm -f "${temporary}"
    printf 'Failed to generate %s.\n' "${destination}" >&2
    exit 1
  fi
  chown "${APP_UID}:${APP_GID}" "${temporary}"
  chmod 0400 "${temporary}"
  mv "${temporary}" "${destination}"
}

make_random_secret "${SECRETS_PATH}/session-secret" 48
make_random_secret "${SECRETS_PATH}/import-token" 32

DEVICE_TOKEN_TRANSFER="${SECRETS_PATH}/device-token-${DEVICE_ID}"
if [ ! -e "${DEVICE_TOKEN_TRANSFER}" ]; then
  if [ -e "${SECRETS_PATH}/device-tokens.json" ]; then
    printf '%s\n' "device-tokens.json exists without the protected transfer file; refusing to expose or replace the existing bearer." >&2
    exit 1
  fi
  temporary=$(mktemp --tmpdir="${SECRETS_PATH}" ".device-token.new.XXXXXXXXXX")
  if ! openssl rand -hex 32 >"${temporary}"; then
    rm -f "${temporary}"
    printf '%s\n' "Failed to generate the device bearer." >&2
    exit 1
  fi
  chown root:root "${temporary}"
  chmod 0400 "${temporary}"
  mv "${temporary}" "${DEVICE_TOKEN_TRANSFER}"
fi

if [ ! -e "${SECRETS_PATH}/device-tokens.json" ]; then
  DEVICE_TOKEN=$(tr -d '\r\n' <"${DEVICE_TOKEN_TRANSFER}")
  case "${DEVICE_TOKEN}" in
    ""|*[!0-9a-f]*)
      unset DEVICE_TOKEN
      printf '%s\n' "Protected device bearer file is invalid." >&2
      exit 1
      ;;
  esac
  if [ "${#DEVICE_TOKEN}" -ne 64 ]; then
    unset DEVICE_TOKEN
    printf '%s\n' "Protected device bearer file has the wrong length." >&2
    exit 1
  fi
  temporary=$(mktemp --tmpdir="${SECRETS_PATH}" ".device-tokens.new.XXXXXXXXXX")
  printf '{"%s":"%s"}\n' "${DEVICE_ID}" "${DEVICE_TOKEN}" >"${temporary}"
  chown "${APP_UID}:${APP_GID}" "${temporary}"
  chmod 0400 "${temporary}"
  mv "${temporary}" "${SECRETS_PATH}/device-tokens.json"
  unset DEVICE_TOKEN
fi

ARCHIVE_SENTINEL="${ARCHIVE_PATH}/.comma-companion-archive"
if [ -L "${ARCHIVE_SENTINEL}" ] \
  || { [ -e "${ARCHIVE_SENTINEL}" ] && [ ! -f "${ARCHIVE_SENTINEL}" ]; }; then
  printf '%s\n' "Archive sentinel must be a regular non-symlink file." >&2
  exit 1
fi
if [ ! -e "${ARCHIVE_SENTINEL}" ]; then
  temporary=$(mktemp --tmpdir="${ARCHIVE_PATH}" ".archive-sentinel.new.XXXXXXXXXX")
  printf '%s\n' "Comma Companion archive mount sentinel" >"${temporary}"
  chown "${APP_UID}:${APP_GID}" "${temporary}" 2>/dev/null || true
  chmod 0600 "${temporary}" 2>/dev/null || true
  mv "${temporary}" "${ARCHIVE_SENTINEL}"
fi

if [ -e "${SECRETS_PATH}/admin-password-hash" ] \
  && [ -n "${ADMIN_PASSWORD_FILE_PATH}" ]
then
  printf '%s\n' \
    "Administrator hash already exists; refusing an unused plaintext password file." >&2
  exit 1
fi
if [ ! -e "${SECRETS_PATH}/admin-password-hash" ]; then
  temporary=$(mktemp --tmpdir="${SECRETS_PATH}" ".admin-hash.new.XXXXXXXXXX")
  if [ -n "${ADMIN_PASSWORD_FILE_PATH}" ]; then
    if ! (
      cd "${SERVICE_ROOT}"
      docker compose run --rm --no-deps \
        --entrypoint comma-companion-hash-password companion \
        <"${ADMIN_PASSWORD_FILE_PATH}"
    ) >"${temporary}"; then
      rm -f "${temporary}"
      printf '%s\n' "Failed to hash the file-supplied administrator password." >&2
      exit 1
    fi
  else
  restore_tty() {
    stty echo 2>/dev/null || true
  }
  trap restore_tty EXIT HUP INT TERM
  printf '%s' "Administrator password: " >&2
  stty -echo
  IFS= read -r ADMIN_PASSWORD
  stty echo
  printf '\n%s' "Confirm administrator password: " >&2
  stty -echo
  IFS= read -r ADMIN_PASSWORD_CONFIRM
  stty echo
  printf '\n' >&2
  trap - EXIT HUP INT TERM
  if [ -z "${ADMIN_PASSWORD}" ] || [ "${ADMIN_PASSWORD}" != "${ADMIN_PASSWORD_CONFIRM}" ]; then
    unset ADMIN_PASSWORD ADMIN_PASSWORD_CONFIRM
    rm -f "${temporary}"
    printf '%s\n' "Passwords were empty or did not match." >&2
    exit 1
  fi
  if ! (
    cd "${SERVICE_ROOT}"
    printf '%s\n' "${ADMIN_PASSWORD}" \
      | docker compose run --rm --no-deps \
        --entrypoint comma-companion-hash-password companion
  ) >"${temporary}"; then
    rm -f "${temporary}"
    unset ADMIN_PASSWORD ADMIN_PASSWORD_CONFIRM
    printf '%s\n' "Failed to hash the administrator password." >&2
    exit 1
  fi
  unset ADMIN_PASSWORD ADMIN_PASSWORD_CONFIRM
  fi
  if ! grep -Eq '^\$argon2id\$v=' "${temporary}"; then
    rm -f "${temporary}"
    printf '%s\n' "Password helper returned an invalid Argon2id hash." >&2
    exit 1
  fi
  chown "${APP_UID}:${APP_GID}" "${temporary}"
  chmod 0400 "${temporary}"
  mv "${temporary}" "${SECRETS_PATH}/admin-password-hash"
  if [ -n "${ADMIN_PASSWORD_FILE_PATH}" ]; then
    rm -f -- "${ADMIN_PASSWORD_FILE_PATH}"
    if [ -e "${ADMIN_PASSWORD_FILE_PATH}" ]; then
      printf '%s\n' \
        "Administrator password file could not be consumed and removed." >&2
      exit 1
    fi
    ADMIN_PASSWORD_FILE_PATH=
    ADMIN_PASSWORD_FILE_VALUE=
    unset COMPANION_ADMIN_PASSWORD_FILE
  fi
fi

python3 - \
  "${SECRETS_PATH}/session-secret" \
  "${SECRETS_PATH}/import-token" \
  "${SECRETS_PATH}/device-tokens.json" \
  "${SECRETS_PATH}/admin-password-hash" \
  "${DEVICE_TOKEN_TRANSFER}" \
  "${DEVICE_ID}" \
  "${APP_UID}" \
  "${APP_GID}" <<'PY'
import json
import os
import re
import stat
import sys
from pathlib import Path

session_path, import_path, token_map_path, admin_path, transfer_path = map(
  Path,
  sys.argv[1:6],
)
device_id = sys.argv[6]
app_uid = int(sys.argv[7])
app_gid = int(sys.argv[8])


def checked_text(
  path: Path,
  *,
  uid: int,
  gid: int,
  mode: int,
) -> str:
  metadata = path.lstat()
  if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
    raise SystemExit(f"secret is not a regular non-symlink file: {path}")
  actual = (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode))
  expected = (uid, gid, mode)
  if actual != expected:
    raise SystemExit(
      f"secret metadata mismatch for {path}: expected {expected}, found {actual}",
    )
  return path.read_text(encoding="utf-8").rstrip("\r\n")


session = checked_text(session_path, uid=app_uid, gid=app_gid, mode=0o400)
import_token = checked_text(import_path, uid=app_uid, gid=app_gid, mode=0o400)
token_map_text = checked_text(
  token_map_path,
  uid=app_uid,
  gid=app_gid,
  mode=0o400,
)
admin_hash = checked_text(admin_path, uid=app_uid, gid=app_gid, mode=0o400)
transfer = checked_text(transfer_path, uid=0, gid=0, mode=0o400)

if re.fullmatch(r"[0-9a-f]{96}", session) is None:
  raise SystemExit("session-secret has an invalid format")
if re.fullmatch(r"[0-9a-f]{64}", import_token) is None:
  raise SystemExit("import-token has an invalid format")
if re.fullmatch(r"[0-9a-f]{64}", transfer) is None:
  raise SystemExit("device transfer token has an invalid format")
if re.match(r"^\$argon2id\$v=", admin_hash) is None:
  raise SystemExit("administrator password hash is not Argon2id")
try:
  token_map = json.loads(token_map_text)
except json.JSONDecodeError as error:
  raise SystemExit(f"device token map is invalid JSON: {error}") from error
if (
  not isinstance(token_map, dict)
  or not token_map
  or any(
    not isinstance(key, str)
    or not isinstance(value, str)
    or re.fullmatch(r"[0-9a-f]{64}", value) is None
    for key, value in token_map.items()
  )
):
  raise SystemExit("device token map has an invalid entry")
if token_map.get(device_id) != transfer:
  raise SystemExit("protected transfer token does not match the server token map")
PY

printf '%s\n' "Bootstrap complete."
printf 'Local state: %s\nArchive: %s\nSecrets: %s\n' \
  "${STATE_PATH}" "${ARCHIVE_PATH}" "${SECRETS_PATH}"
printf 'Importer host-port gateway: %s\n' "${PROXY_GATEWAY}"
printf 'Protected device bearer transfer file: %s\n' "${DEVICE_TOKEN_TRANSFER}"
printf '%s\n' \
  "Run: docker compose config --quiet && docker compose up -d --wait --no-build"
