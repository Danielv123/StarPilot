#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ACTION=${1:-check}
NPM_CONTAINER=${NPM_CONTAINER:-nginx-proxy-manager}
NPM_NETWORK=${NPM_NETWORK:-webserver-proxy}
COMPANION_CONTAINER=${COMPANION_CONTAINER:-comma-companion}
ORIGIN_CERT=${ORIGIN_CERT:-/home/danielv/.local/share/comma-companion-origin/origin.pem}
ORIGIN_KEY=${ORIGIN_KEY:-/home/danielv/.local/share/comma-companion-origin/origin.key}
CONFIG_ROOT=/data/nginx/custom/comma-companion
HTTP_TOP_HOOK=/data/nginx/custom/http_top.conf
HTTP_HOOK=/data/nginx/custom/http.conf
HTTP_TOP_INCLUDE="include ${CONFIG_ROOT}/current/http-top.conf;"
HTTP_INCLUDE="include ${CONFIG_ROOT}/current/server.conf;"
LOCK_PATH=/run/comma-companion-npm.lock

PROMOTION_ACTIVE=0
PROMOTION_PREVIOUS=
PROMOTION_RELEASE=
PROMOTION_RESTORE_HOOKS=0
SNAPSHOT_CERT=
SNAPSHOT_KEY=
RENDERED_TEMP=
POLICY_TEMP_DIR=

case "${ACTION}" in
  install|check) ;;
  rollback)
    if [ "$#" -ne 2 ]; then
      printf '%s\n' "Usage: manage.sh rollback <release-id>" >&2
      exit 2
    fi
    ;;
  *)
    printf '%s\n' "Usage: manage.sh [install|check|rollback <release-id>]" >&2
    exit 2
    ;;
esac

case "${NPM_CONTAINER}${NPM_NETWORK}${COMPANION_CONTAINER}" in
  *[!A-Za-z0-9_.-]*)
    printf '%s\n' "Container and network names contain unsafe characters." >&2
    exit 2
    ;;
esac

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf '%s is required.\n' "$1" >&2
    exit 1
  fi
}

for command_name in cp docker flock mktemp openssl python3 sha256sum stat
do
  require_command "${command_name}"
done

if [ "$(id -u)" -ne 0 ]; then
  printf '%s\n' "Run this NPM configuration manager as root." >&2
  exit 1
fi
if [ ! -d /run ] \
  || [ -L /run ] \
  || [ "$(stat -c %u /run)" -ne 0 ] \
  || [ $((0$(stat -c %a /run) & 0022)) -ne 0 ]
then
  printf '%s\n' "/run must be an existing root-owned non-writable directory." >&2
  exit 1
fi
if [ -L "${LOCK_PATH}" ] \
  || { [ -e "${LOCK_PATH}" ] && [ ! -f "${LOCK_PATH}" ]; }
then
  printf '%s\n' "NPM lock path must be a regular non-symlink file." >&2
  exit 1
fi
umask 077
exec 9>>"${LOCK_PATH}"
if [ "$(stat -Lc %u:%g:%a:%h "/proc/$$/fd/9")" != "0:0:600:1" ]; then
  printf '%s\n' "NPM lock file metadata is unsafe." >&2
  exit 1
fi
if ! flock -n 9; then
  printf '%s\n' "Another NPM configuration action is already running." >&2
  exit 1
fi

if [ "$(docker inspect --format '{{.State.Running}}' "${NPM_CONTAINER}" 2>/dev/null || true)" != "true" ]; then
  printf 'NPM container is not running: %s\n' "${NPM_CONTAINER}" >&2
  exit 1
fi
NPM_VERSION=$(
  docker exec "${NPM_CONTAINER}" \
    sh -c 'printf "%s" "${NPM_BUILD_VERSION:-}"' 2>/dev/null \
    || true
)
if ! python3 - "${NPM_VERSION}" <<'PY'
import re
import sys

match = re.fullmatch(r"2\.15\.(\d+)", sys.argv[1])
if match is None or int(match.group(1)) < 1:
  raise SystemExit(1)
PY
then
  printf 'Expected Nginx Proxy Manager 2.15.1 or newer 2.15.x, found %s.\n' \
    "${NPM_VERSION:-unknown}" >&2
  exit 1
fi

NPM_PUID=$(
  docker exec "${NPM_CONTAINER}" \
    sh -c 'printf "%s" "${PUID:-0}"' 2>/dev/null \
    || true
)
NPM_PGID=$(
  docker exec "${NPM_CONTAINER}" \
    sh -c 'printf "%s" "${PGID:-0}"' 2>/dev/null \
    || true
)
if ! python3 - "${NPM_PUID}:${NPM_PGID}" <<'PY'
import re
import sys

if re.fullmatch(r"\d+:\d+", sys.argv[1]) is None:
  raise SystemExit(1)
PY
then
  printf 'NPM PUID/PGID are invalid: %s:%s\n' \
    "${NPM_PUID:-missing}" "${NPM_PGID:-missing}" >&2
  exit 1
fi

NPM_IMAGE_ID=$(docker inspect --format '{{.Image}}' "${NPM_CONTAINER}")
if ! printf '%s\n' "${NPM_IMAGE_ID}" \
  | grep -Eq '^sha256:[0-9a-f]{64}$'
then
  printf 'NPM container has an invalid image ID: %s\n' "${NPM_IMAGE_ID}" >&2
  exit 1
fi
if [ -n "${NPM_EXPECTED_IMAGE_ID:-}" ] \
  && [ "${NPM_IMAGE_ID}" != "${NPM_EXPECTED_IMAGE_ID}" ]
then
  printf 'NPM image ID differs from NPM_EXPECTED_IMAGE_ID: %s\n' \
    "${NPM_IMAGE_ID}" >&2
  exit 1
fi

NPM_NETWORK_ADDRESS=$(
  docker inspect "${NPM_CONTAINER}" \
    | python3 -c '
import ipaddress
import json
import sys

container = json.load(sys.stdin)[0]
network = container["NetworkSettings"]["Networks"].get(sys.argv[1])
address = network.get("IPAddress", "") if network else ""
if address:
  address = str(ipaddress.ip_address(address))
print(address)
' "${NPM_NETWORK}"
)
if [ -z "${NPM_NETWORK_ADDRESS}" ]; then
  printf 'NPM container is not attached to %s with an IP address.\n' \
    "${NPM_NETWORK}" >&2
  exit 1
fi

container_root() {
  docker exec --user 0 "${NPM_CONTAINER}" "$@"
}

container_nginx() {
  docker exec --user "${NPM_PUID}:${NPM_PGID}" "${NPM_CONTAINER}" "$@"
}

NPM_NGINX_BUILD=$(
  container_nginx nginx -v 2>&1 \
    || true
)
if ! python3 - "${NPM_NGINX_BUILD}" <<'PY'
import re
import sys

match = re.search(r"openresty/(\d+)\.(\d+)\.(\d+)\.(\d+)", sys.argv[1])
if match is None or tuple(map(int, match.groups())) < (1, 29, 2, 5):
  raise SystemExit(1)
PY
then
  printf 'NPM OpenResty build is older than reviewed 1.29.2.5: %s\n' \
    "${NPM_NGINX_BUILD:-unknown}" >&2
  exit 1
fi

valid_release_id() {
  printf '%s\n' "$1" \
    | grep -Eq '^[0-9]{8}T[0-9]{6}Z-[1-9][0-9]*$'
}

validated_target() {
  target=$1
  case "${target}" in
    releases/*) release_id=${target#releases/} ;;
    *)
      printf 'NPM release target is unsafe: %s\n' "${target:-missing}" >&2
      return 1
      ;;
  esac
  if ! valid_release_id "${release_id}" \
    || [ "${target}" != "releases/${release_id}" ]
  then
    printf 'NPM release target is unsafe: %s\n' "${target}" >&2
    return 1
  fi
  release_path="${CONFIG_ROOT}/${target}"
  if ! container_root test -d "${release_path}" \
    || container_root test -L "${release_path}"
  then
    printf 'NPM release is not a real directory: %s\n' "${target}" >&2
    return 1
  fi
  printf '%s\n' "${release_path}"
}

current_target() {
  target=$(container_root readlink "${CONFIG_ROOT}/current" 2>/dev/null || true)
  if [ -z "${target}" ] \
    && { container_root test -e "${CONFIG_ROOT}/current" \
      || container_root test -L "${CONFIG_ROOT}/current"; }
  then
    printf '%s\n' \
      "NPM current path exists but is not a readable release symlink." >&2
    return 1
  fi
  printf '%s\n' "${target}"
}

verify_templates() {
  python3 "${SCRIPT_DIR}/verify_config.py" "${SCRIPT_DIR}"
}

verify_origin_pair() {
  certificate=$1
  key=$2
  openssl x509 -in "${certificate}" -noout -checkend 604800 >/dev/null
  openssl x509 -in "${certificate}" -noout -checkhost comma.danielv.no >/dev/null
  certificate_key=$(
    openssl x509 -in "${certificate}" -pubkey -noout \
      | openssl pkey -pubin -outform DER 2>/dev/null \
      | openssl dgst -sha256
  )
  private_key=$(
    openssl pkey -in "${key}" -pubout -outform DER 2>/dev/null \
      | openssl dgst -sha256
  )
  if [ "${certificate_key}" != "${private_key}" ]; then
    printf '%s\n' "Origin certificate and private key do not match." >&2
    return 1
  fi
}

snapshot_origin_pair() {
  for path in "${ORIGIN_CERT}" "${ORIGIN_KEY}"
  do
    if [ -L "${path}" ] || [ ! -f "${path}" ]; then
      printf 'Origin TLS input must be a regular non-symlink file: %s\n' \
        "${path}" >&2
      return 1
    fi
    if [ "$(stat -c %u:%g "${path}")" != "0:0" ]; then
      printf 'Origin TLS input must be owned by root:root: %s\n' \
        "${path}" >&2
      return 1
    fi
  done
  case "$(stat -c %a "${ORIGIN_CERT}")" in
    400|600|644) ;;
    *)
      printf '%s\n' "Origin certificate must have mode 0400, 0600, or 0644." >&2
      return 1
      ;;
  esac
  case "$(stat -c %a "${ORIGIN_KEY}")" in
    400|600) ;;
    *)
      printf '%s\n' "Origin private key must have mode 0400 or 0600." >&2
      return 1
      ;;
  esac
  SNAPSHOT_CERT=$(mktemp /run/comma-companion-origin-cert.XXXXXXXXXX)
  SNAPSHOT_KEY=$(mktemp /run/comma-companion-origin-key.XXXXXXXXXX)
  chmod 0600 "${SNAPSHOT_CERT}" "${SNAPSHOT_KEY}"
  cp --no-dereference -- "${ORIGIN_CERT}" "${SNAPSHOT_CERT}"
  cp --no-dereference -- "${ORIGIN_KEY}" "${SNAPSHOT_KEY}"
  chown root:root "${SNAPSHOT_CERT}" "${SNAPSHOT_KEY}"
  chmod 0600 "${SNAPSHOT_CERT}"
  chmod 0400 "${SNAPSHOT_KEY}"
  verify_origin_pair "${SNAPSHOT_CERT}" "${SNAPSHOT_KEY}"
}

verify_release_certificate() {
  release=$1
  certificate="${release}/tls/origin.pem"
  key="${release}/tls/origin.key"
  for path in "${certificate}" "${key}"
  do
    if ! container_root test -f "${path}" \
      || container_root test -L "${path}" \
      || [ "$(container_root stat -c %h "${path}")" != "1" ]
    then
      printf 'Deployed TLS input is unsafe: %s\n' "${path}" >&2
      return 1
    fi
  done
  if [ "$(container_root stat -c %u:%g:%a "${certificate}")" \
      != "${NPM_PUID}:${NPM_PGID}:400" ] \
    || [ "$(container_root stat -c %u:%g:%a "${key}")" \
      != "${NPM_PUID}:${NPM_PGID}:400" ]
  then
    printf '%s\n' "Deployed origin certificate/key metadata is unsafe." >&2
    return 1
  fi
  container_nginx sh -ec '
    certificate=$1
    key=$2
    test -r "${certificate}" -a -r "${key}"
    openssl x509 -in "${certificate}" -noout -checkend 604800 >/dev/null
    openssl x509 -in "${certificate}" -noout -checkhost comma.danielv.no >/dev/null
    certificate_key=$(
      openssl x509 -in "${certificate}" -pubkey -noout |
        openssl pkey -pubin -outform DER 2>/dev/null |
        openssl dgst -sha256
    )
    private_key=$(
      openssl pkey -in "${key}" -pubout -outform DER 2>/dev/null |
        openssl dgst -sha256
    )
    test "${certificate_key}" = "${private_key}"
  ' sh "${certificate}" "${key}"
}

clear_policy_temp() {
  if [ -n "${POLICY_TEMP_DIR}" ]; then
    case "${POLICY_TEMP_DIR}" in
      /run/comma-companion-npm-policy.*)
        rm -rf -- "${POLICY_TEMP_DIR}"
        ;;
      *)
        printf 'Refusing unsafe policy temporary path: %s\n' \
          "${POLICY_TEMP_DIR}" >&2
        return 1
        ;;
    esac
    POLICY_TEMP_DIR=
  fi
}

verify_release_policy() {
  release=$1
  if ! container_root test -f "${release}/template.sha256" \
    || container_root test -L "${release}/template.sha256"
  then
    printf '%s\n' "Release template checksum manifest is missing or unsafe." >&2
    return 1
  fi
  container_root sh -ec '
    cd "$1"
    sha256sum --check --strict template.sha256
  ' sh "${release}" >/dev/null

  POLICY_TEMP_DIR=$(mktemp -d /run/comma-companion-npm-policy.XXXXXXXXXX)
  chmod 0700 "${POLICY_TEMP_DIR}"
  for name in \
    http-top.conf \
    server.conf \
    cloudflare-source.geo \
    cloudflare-realip.conf
  do
    if ! container_root test -f "${release}/${name}" \
      || container_root test -L "${release}/${name}"
    then
      printf 'Release policy file is missing or unsafe: %s\n' "${name}" >&2
      return 1
    fi
    docker cp "${NPM_CONTAINER}:${release}/${name}" "${POLICY_TEMP_DIR}/${name}"
  done
  python3 "${SCRIPT_DIR}/verify_config.py" "${POLICY_TEMP_DIR}"
  clear_policy_temp
}

verify_release_matches_source() {
  release=$1
  for name in \
    http-top.conf \
    server.conf \
    cloudflare-source.geo \
    cloudflare-realip.conf
  do
    expected=$(sha256sum "${SCRIPT_DIR}/${name}" | awk '{print $1}')
    actual=$(container_root sha256sum "${release}/${name}" | awk '{print $1}')
    if [ "${actual}" != "${expected}" ]; then
      printf 'Copied NPM policy differs from verified source: %s\n' \
        "${name}" >&2
      return 1
    fi
  done
}

verify_rendered_nginx() {
  RENDERED_TEMP=$(mktemp /run/comma-companion-nginx-rendered.XXXXXXXXXX)
  chmod 0600 "${RENDERED_TEMP}"
  if ! container_nginx nginx -T >"${RENDERED_TEMP}" 2>&1; then
    printf '%s\n' "nginx -T failed as the configured NPM UID/GID." >&2
    return 1
  fi
  if ! python3 - "${RENDERED_TEMP}" "${NPM_NGINX_BUILD}" <<'PY'
import re
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
build = sys.argv[2]


def without_comments(value: str) -> str:
  output = []
  quote = None
  escaped = False
  comment = False
  for character in value:
    if comment:
      if character == "\n":
        comment = False
        output.append(character)
      continue
    if escaped:
      output.append(character)
      escaped = False
      continue
    if character == "\\" and quote is not None:
      output.append(character)
      escaped = True
      continue
    if character in {"'", '"'}:
      if quote == character:
        quote = None
      elif quote is None:
        quote = character
      output.append(character)
      continue
    if character == "#" and quote is None:
      comment = True
      continue
    output.append(character)
  return "".join(output)


def map_bodies(value: str) -> list[str]:
  bodies = []
  for match in re.finditer(r"\bmap\s+\S+\s+\S+\s*\{", value):
    opening = match.end() - 1
    depth = 1
    quote = None
    escaped = False
    for index in range(opening + 1, len(value)):
      character = value[index]
      if escaped:
        escaped = False
        continue
      if character == "\\" and quote is not None:
        escaped = True
        continue
      if character in {"'", '"'}:
        if quote == character:
          quote = None
        elif quote is None:
          quote = character
        continue
      if quote is not None:
        continue
      if character == "{":
        depth += 1
      elif character == "}":
        depth -= 1
        if depth == 0:
          bodies.append(value[opening + 1:index])
          break
    else:
      raise SystemExit("effective config contains an unclosed map block")
  return bodies


active_text = without_comments(text)
if "conflicting server name" in text.lower():
  raise SystemExit("rendered config contains a conflicting server name warning")
owners = [
  line
  for line in text.splitlines()
  if re.match(r"^\s*server_name\s+", line)
  and "comma.danielv.no" in line.split(";", 1)[0].split()[1:]
]
if len(owners) != 1:
  raise SystemExit(
    f"expected exactly one comma.danielv.no server_name owner, found {len(owners)}",
  )
required_once = (
  "include /data/nginx/custom/comma-companion/current/http-top.conf;",
  "include /data/nginx/custom/comma-companion/current/server.conf;",
  "resolver 127.0.0.11 valid=10s ipv6=off;",
  "resolver_timeout 5s;",
)
for directive in required_once:
  if text.count(directive) != 1:
    raise SystemExit(f"rendered directive is not present exactly once: {directive}")
if re.search(r"^\s*ssi\s+on\s*;", text, flags=re.MULTILINE | re.IGNORECASE):
  raise SystemExit(f"{build} patch-watch gate rejects effective `ssi on`")
if re.search(r"^\s*slice\s+[^;]+;", text, flags=re.MULTILINE | re.IGNORECASE):
  raise SystemExit(f"{build} patch-watch gate rejects effective `slice`")
for body in map_bodies(active_text):
  if re.search(r"""(?:^|;)\s*["']?~\*?""", body):
    raise SystemExit(f"{build} patch-watch gate rejects regex map keys")
if re.search(
  r"^\s*proxy_http_version\s+2(?:\.0)?\s*;",
  text,
  flags=re.MULTILINE | re.IGNORECASE,
):
  raise SystemExit(f"{build} patch-watch gate rejects proxy HTTP/2")
if re.search(r"^\s*grpc_pass\s+", text, flags=re.MULTILINE | re.IGNORECASE):
  raise SystemExit(f"{build} patch-watch gate rejects grpc_pass")
if re.search(r"^\s*source_charset\s+", text, flags=re.MULTILINE | re.IGNORECASE):
  raise SystemExit(f"{build} patch-watch gate rejects source_charset")
PY
  then
    return 1
  fi
  rm -f -- "${RENDERED_TEMP}"
  RENDERED_TEMP=
}

verify_backend_proxy_contract() {
  if [ "$(docker inspect --format '{{.State.Running}}' "${COMPANION_CONTAINER}" 2>/dev/null || true)" != "true" ]; then
    printf 'Companion API container is not running: %s\n' \
      "${COMPANION_CONTAINER}" >&2
    return 1
  fi
  forwarded=$(
    docker inspect "${COMPANION_CONTAINER}" \
      | python3 -c '
import json
import sys

container = json.load(sys.stdin)[0]
values = {}
for item in container.get("Config", {}).get("Env", []):
  key, separator, value = item.partition("=")
  if separator:
    values[key] = value
print(values.get("FORWARDED_ALLOW_IPS", ""))
'
  )
  if [ "${forwarded}" != "${NPM_NETWORK_ADDRESS}" ]; then
    printf 'Companion FORWARDED_ALLOW_IPS must equal NPM address %s, found %s.\n' \
      "${NPM_NETWORK_ADDRESS}" "${forwarded:-missing}" >&2
    return 1
  fi
  docker exec "${NPM_CONTAINER}" node -e '
    const http = require("http");
    let completed = false;
    const request = http.get({
      host: "comma-companion",
      port: 8000,
      path: "/api/v1/health",
      timeout: 5000,
      headers: {Host: "comma.danielv.no"},
    }, (response) => {
      let body = "";
      let bodyBytes = 0;
      response.setEncoding("utf8");
      response.on("data", (chunk) => {
        bodyBytes += Buffer.byteLength(chunk);
        if (bodyBytes > 16384) {
          request.destroy(new Error("health response too large"));
          return;
        }
        body += chunk;
      });
      response.on("end", () => {
        completed = true;
        clearTimeout(deadline);
        try {
          const payload = JSON.parse(body);
          if (
            response.statusCode !== 200
            || payload.status !== "ok"
            || payload.service !== "comma-companion-api"
          ) {
            process.exit(1);
          }
        } catch {
          process.exit(1);
        }
      });
    });
    const deadline = setTimeout(
      () => request.destroy(new Error("hard timeout")),
      7000,
    );
    request.on("timeout", () => request.destroy(new Error("timeout")));
    request.on("error", () => {
      clearTimeout(deadline);
      if (!completed) process.exit(1);
    });
  '
}

restore_hook() {
  hook=$1
  backup=$2
  if container_root test -e "${backup}.absent"; then
    container_root rm -f "${hook}"
  elif container_root test -e "${backup}"; then
    container_root rm -f "${hook}"
    container_root cp -a "${backup}" "${hook}"
  else
    printf 'Hook backup is missing: %s\n' "${backup}" >&2
    return 1
  fi
}

restore_activation() {
  status=0
  restore_link="${CONFIG_ROOT}/current.restore.$$"
  container_root rm -f "${restore_link}" || status=1
  if [ -n "${PROMOTION_PREVIOUS}" ]; then
    container_root ln -s "${PROMOTION_PREVIOUS}" "${restore_link}" || status=1
    container_root mv -Tf "${restore_link}" "${CONFIG_ROOT}/current" || status=1
  else
    container_root rm -f "${CONFIG_ROOT}/current" || status=1
  fi
  if [ "${PROMOTION_RESTORE_HOOKS}" -eq 1 ]; then
    restore_hook \
      "${HTTP_TOP_HOOK}" \
      "${PROMOTION_RELEASE}/hook-backup/http_top.conf" || status=1
    restore_hook \
      "${HTTP_HOOK}" \
      "${PROMOTION_RELEASE}/hook-backup/http.conf" || status=1
  fi
  container_nginx nginx -t || status=1
  container_root nginx -s reload || status=1
  return "${status}"
}

cleanup_temporary_files() {
  for path in "${SNAPSHOT_CERT}" "${SNAPSHOT_KEY}" "${RENDERED_TEMP}"
  do
    if [ -n "${path}" ]; then
      rm -f -- "${path}"
    fi
  done
  clear_policy_temp
}

on_exit() {
  exit_status=$?
  trap - EXIT HUP INT TERM
  if [ "${PROMOTION_ACTIVE}" -eq 1 ]; then
    printf '%s\n' \
      "NPM activation failed; restoring the previous release and hooks." >&2
    set +e
    restore_activation
    restore_status=$?
    set -e
    if [ "${restore_status}" -ne 0 ]; then
      printf '%s\n' \
        "EMERGENCY: automatic NPM restoration was incomplete." >&2
      exit_status=1
    fi
  fi
  cleanup_temporary_files || exit_status=1
  exit "${exit_status}"
}

validate_hook() {
  hook=$1
  managed_path=$2
  if container_root test -L "${hook}"; then
    printf 'NPM singleton hook must not be a symlink: %s\n' "${hook}" >&2
    return 1
  fi
  if container_root test -e "${hook}" \
    && ! container_root test -f "${hook}"
  then
    printf 'NPM singleton hook must be a regular file: %s\n' "${hook}" >&2
    return 1
  fi
  references=$(
    container_root sh -c \
      'test -f "$1" && grep -Fc "$2" "$1" || true' \
      sh "${hook}" "${managed_path}"
  )
  if [ "${references}" -gt 1 ]; then
    printf 'NPM singleton hook contains duplicate managed references: %s\n' \
      "${hook}" >&2
    return 1
  fi
}

append_hook_once() {
  hook=$1
  include_line=$2
  managed_path=$3
  validate_hook "${hook}" "${managed_path}"
  references=$(
    container_root sh -c \
      'test -f "$1" && grep -Fc "$2" "$1" || true' \
      sh "${hook}" "${managed_path}"
  )
  if [ "${references}" -eq 1 ]; then
    if ! container_root grep -Fqx "${include_line}" "${hook}"; then
      printf 'Managed NPM hook reference has unexpected syntax: %s\n' \
        "${hook}" >&2
      return 1
    fi
    return
  fi
  container_root sh -ec '
    hook=$1
    include_line=$2
    temporary=$(mktemp "${hook}.new.XXXXXXXXXX")
    if [ -f "${hook}" ]; then
      cat "${hook}" >"${temporary}"
    fi
    printf "\n# Comma Companion managed include\n%s\n" \
      "${include_line}" >>"${temporary}"
    chown root:root "${temporary}"
    chmod 0644 "${temporary}"
    mv "${temporary}" "${hook}"
  ' sh "${hook}" "${include_line}"
}

verify_current_release() {
  target=$(current_target)
  release=$(validated_target "${target}")
  verify_release_policy "${release}"
  verify_release_certificate "${release}"
}

install_release() {
  verify_templates
  snapshot_origin_pair
  verify_backend_proxy_contract

  release_id=$(date -u +%Y%m%dT%H%M%SZ)-$$
  if ! valid_release_id "${release_id}"; then
    printf 'Generated release ID is invalid: %s\n' "${release_id}" >&2
    exit 1
  fi
  release="${CONFIG_ROOT}/releases/${release_id}"
  previous=$(current_target)
  if [ -n "${previous}" ]; then
    validated_target "${previous}" >/dev/null
  fi
  if container_root test -e "${release}"; then
    printf 'NPM release already exists: %s\n' "${release_id}" >&2
    exit 1
  fi

  validate_hook "${HTTP_TOP_HOOK}" "${CONFIG_ROOT}/current/http-top.conf"
  validate_hook "${HTTP_HOOK}" "${CONFIG_ROOT}/current/server.conf"
  container_root mkdir -p \
    "${CONFIG_ROOT}/releases" \
    "${release}/tls" \
    "${release}/hook-backup"
  container_root chmod 0755 "${CONFIG_ROOT}" "${CONFIG_ROOT}/releases" "${release}"
  container_root chmod 0700 "${release}/hook-backup"
  container_root chown "${NPM_PUID}:${NPM_PGID}" "${release}/tls"
  container_root chmod 0700 "${release}/tls"

  for hook_spec in \
    "${HTTP_TOP_HOOK}:http_top.conf" \
    "${HTTP_HOOK}:http.conf"
  do
    hook=${hook_spec%:*}
    name=${hook_spec##*:}
    if container_root test -e "${hook}"; then
      container_root cp -a "${hook}" "${release}/hook-backup/${name}"
    else
      container_root touch "${release}/hook-backup/${name}.absent"
    fi
  done

  PROMOTION_RELEASE=${release}
  PROMOTION_PREVIOUS=${previous}
  PROMOTION_RESTORE_HOOKS=1
  PROMOTION_ACTIVE=1

  docker cp "${SCRIPT_DIR}/http-top.conf" \
    "${NPM_CONTAINER}:${release}/http-top.conf"
  docker cp "${SCRIPT_DIR}/server.conf" \
    "${NPM_CONTAINER}:${release}/server.conf"
  docker cp "${SCRIPT_DIR}/cloudflare-source.geo" \
    "${NPM_CONTAINER}:${release}/cloudflare-source.geo"
  docker cp "${SCRIPT_DIR}/cloudflare-realip.conf" \
    "${NPM_CONTAINER}:${release}/cloudflare-realip.conf"
  docker cp "${SNAPSHOT_CERT}" "${NPM_CONTAINER}:${release}/tls/origin.pem"
  docker cp "${SNAPSHOT_KEY}" "${NPM_CONTAINER}:${release}/tls/origin.key"
  container_root chown root:root \
    "${release}/http-top.conf" \
    "${release}/server.conf" \
    "${release}/cloudflare-source.geo" \
    "${release}/cloudflare-realip.conf"
  container_root chmod 0444 \
    "${release}/http-top.conf" \
    "${release}/server.conf" \
    "${release}/cloudflare-source.geo" \
    "${release}/cloudflare-realip.conf"
  container_root chown "${NPM_PUID}:${NPM_PGID}" \
    "${release}/tls/origin.pem" \
    "${release}/tls/origin.key"
  container_root chmod 0400 \
    "${release}/tls/origin.pem" \
    "${release}/tls/origin.key"
  container_root sh -ec '
    cd "$1"
    sha256sum \
      http-top.conf \
      server.conf \
      cloudflare-source.geo \
      cloudflare-realip.conf \
      >template.sha256
    chown root:root template.sha256
    chmod 0444 template.sha256
  ' sh "${release}"

  verify_release_matches_source "${release}"
  verify_release_policy "${release}"
  verify_release_certificate "${release}"

  promotion_link="${CONFIG_ROOT}/current.install.$$"
  container_root rm -f "${promotion_link}"
  container_root ln -s "releases/${release_id}" "${promotion_link}"
  container_root mv -Tf "${promotion_link}" "${CONFIG_ROOT}/current"
  append_hook_once \
    "${HTTP_TOP_HOOK}" \
    "${HTTP_TOP_INCLUDE}" \
    "${CONFIG_ROOT}/current/http-top.conf"
  append_hook_once \
    "${HTTP_HOOK}" \
    "${HTTP_INCLUDE}" \
    "${CONFIG_ROOT}/current/server.conf"

  container_nginx nginx -t
  verify_current_release
  verify_rendered_nginx
  container_root nginx -s reload
  verify_rendered_nginx
  verify_backend_proxy_contract

  PROMOTION_ACTIVE=0
  printf 'Installed NPM Comma Companion release: %s\n' "${release_id}"
  if [ -n "${previous}" ]; then
    previous_id=${previous#releases/}
    printf 'Rollback command: sudo sh %s rollback %s\n' \
      "$0" "${previous_id}"
  else
    printf '%s\n' \
      "This is the first managed release; no prior release exists to activate."
  fi
}

rollback_release() {
  release_id=$2
  if ! valid_release_id "${release_id}"; then
    printf 'Rollback release ID is invalid: %s\n' "${release_id}" >&2
    exit 2
  fi
  target="releases/${release_id}"
  release=$(validated_target "${target}")
  previous=$(current_target)
  previous_release=$(validated_target "${previous}")
  if [ "${target}" = "${previous}" ]; then
    printf 'Release is already active: %s\n' "${release_id}" >&2
    exit 1
  fi

  verify_templates
  verify_release_policy "${release}"
  verify_release_certificate "${release}"
  verify_backend_proxy_contract

  PROMOTION_RELEASE=${release}
  PROMOTION_PREVIOUS=${previous}
  PROMOTION_RESTORE_HOOKS=0
  PROMOTION_ACTIVE=1

  promotion_link="${CONFIG_ROOT}/current.rollback.$$"
  container_root rm -f "${promotion_link}"
  container_root ln -s "${target}" "${promotion_link}"
  container_root mv -Tf "${promotion_link}" "${CONFIG_ROOT}/current"
  container_nginx nginx -t
  verify_current_release
  verify_rendered_nginx
  container_root nginx -s reload
  verify_rendered_nginx
  verify_backend_proxy_contract

  PROMOTION_ACTIVE=0
  printf 'Activated NPM Comma Companion release: %s\n' "${release_id}"
  printf 'Previous release: %s\n' "${previous#releases/}"
}

check_release() {
  verify_templates
  verify_current_release
  container_nginx nginx -t
  verify_rendered_nginx
  verify_backend_proxy_contract
  printf 'NPM Comma Companion configuration is valid on %s (%s, %s, uid %s:%s, image %s).\n' \
    "${NPM_CONTAINER}" \
    "${NPM_VERSION}" \
    "${NPM_NGINX_BUILD}" \
    "${NPM_PUID}" \
    "${NPM_PGID}" \
    "${NPM_IMAGE_ID}"
}

trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
trap on_exit EXIT

case "${ACTION}" in
  install)
    install_release
    ;;
  rollback)
    rollback_release "$@"
    ;;
  check)
    check_release
    ;;
esac
