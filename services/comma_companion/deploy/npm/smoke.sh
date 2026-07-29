#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
DOMAIN=${COMPANION_PUBLIC_HOST:-comma.danielv.no}
ORIGIN_ADDRESS=${COMPANION_ORIGIN_SMOKE_ADDRESS:-127.0.0.1}

case "${DOMAIN}" in
  ""|*[!A-Za-z0-9.-]*)
    printf 'Unsafe public host: %s\n' "${DOMAIN}" >&2
    exit 2
    ;;
esac
case "${ORIGIN_ADDRESS}" in
  ""|*[!0-9A-Fa-f:.]*)
    printf 'Unsafe origin smoke address: %s\n' "${ORIGIN_ADDRESS}" >&2
    exit 2
    ;;
esac

for command_name in awk curl dd openssl python3
do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    printf '%s is required.\n' "${command_name}" >&2
    exit 1
  fi
done

if [ "$(id -u)" -ne 0 ]; then
  printf '%s\n' "Run the NPM smoke test as root." >&2
  exit 1
fi

headers=$(mktemp)
body=$(mktemp)
large_body=$(mktemp)
cleanup() {
  rm -f -- "${headers}" "${body}" "${large_body}"
}
trap cleanup EXIT HUP INT TERM
chmod 0600 "${headers}" "${body}" "${large_body}"
dd if=/dev/zero of="${large_body}" bs=1048576 count=3 status=none

sh "${SCRIPT_DIR}/manage.sh" check

direct_status=$(
  curl --insecure --silent --show-error \
    --noproxy "*" \
    --resolve "${DOMAIN}:443:${ORIGIN_ADDRESS}" \
    --output /dev/null \
    --write-out "%{http_code}" \
    --max-time 20 \
    --header "CF-Connecting-IP: 203.0.113.7" \
    "https://${DOMAIN}/api/v1/health"
)
if [ "${direct_status}" != "403" ]; then
  printf 'Direct-origin spoof test expected 403, received %s.\n' \
    "${direct_status}" >&2
  exit 1
fi

trace_ip=$(
  curl -4 --fail --silent --show-error \
    --max-time 20 \
    https://www.cloudflare.com/cdn-cgi/trace \
    | awk -F= '$1 == "ip" { print $2; exit }'
)
python3 - "${trace_ip}" <<'PY'
import ipaddress
import sys

try:
  value = ipaddress.ip_address(sys.argv[1])
except ValueError as error:
  raise SystemExit(f"Cloudflare trace did not return a valid IP: {error}")
if value.version != 4:
  raise SystemExit("the IPv4 smoke path did not return an IPv4 visitor address")
PY

smoke_nonce=$(date -u +%s)-$$
health_status=$(
  curl -4 --silent --show-error \
    --dump-header "${headers}" \
    --output "${body}" \
    --write-out "%{http_code}" \
    --max-time 20 \
    --header "Cache-Control: no-cache" \
    "https://${DOMAIN}/api/v1/health?smoke=${smoke_nonce}"
)
if [ "${health_status}" != "200" ]; then
  printf 'Public health expected 200, received %s.\n' "${health_status}" >&2
  exit 1
fi
python3 - "${body}" <<'PY'
import json
import sys
from pathlib import Path

payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if payload.get("status") != "ok":
  raise SystemExit("public health did not return status=ok")
if payload.get("service") != "comma-companion-api":
  raise SystemExit("public health returned an unexpected service identity")
PY
reported_ip=$(
  awk '
    BEGIN { IGNORECASE = 1 }
    /^X-Comma-Client-IP:/ {
      value = $2
      gsub("\r", "", value)
    }
    END { print value }
  ' "${headers}"
)
if [ "${reported_ip}" != "${trace_ip}" ]; then
  printf 'Cloudflare visitor restoration mismatch: trace=%s proxy=%s.\n' \
    "${trace_ip}" "${reported_ip:-missing}" >&2
  exit 1
fi

readiness_status=$(
  curl -4 --silent --show-error \
    --output /dev/null \
    --write-out "%{http_code}" \
    --max-time 20 \
    "https://${DOMAIN}/api/v1/readiness?smoke=${smoke_nonce}"
)
if [ "${readiness_status}" != "404" ]; then
  printf 'Public readiness expected 404, received %s.\n' \
    "${readiness_status}" >&2
  exit 1
fi

login_status=$(
  curl -4 --silent --show-error \
    --output /dev/null \
    --write-out "%{http_code}" \
    --max-time 30 \
    --request POST \
    --header "Content-Type: application/json" \
    --header "Expect:" \
    --data-binary "@${large_body}" \
    "https://${DOMAIN}/api/v1/auth/login"
)
if [ "${login_status}" != "413" ]; then
  printf 'Oversized buffered login expected 413, received %s.\n' \
    "${login_status}" >&2
  exit 1
fi

upload_id=$(openssl rand -hex 16)
upload_status=$(
  curl -4 --silent --show-error \
    --output /dev/null \
    --write-out "%{http_code}" \
    --max-time 60 \
    --request PATCH \
    --header "Content-Type: application/offset+octet-stream" \
    --header "Upload-Offset: 0" \
    --header "Expect:" \
    --data-binary "@${large_body}" \
    "https://${DOMAIN}/api/v1/uploads/${upload_id}"
)
case "${upload_status}" in
  401|403|404|409) ;;
  413)
    printf '%s\n' \
      "Upload-item path inherited the short JSON body-size limit (413)." >&2
    exit 1
    ;;
  *)
    printf 'Unexpected unauthenticated upload smoke status: %s.\n' \
      "${upload_status}" >&2
    exit 1
    ;;
esac

printf '%s\n' \
  "NPM smoke passed: direct spoof denied, Cloudflare visitor restored, readiness hidden, and upload policy scoped."
