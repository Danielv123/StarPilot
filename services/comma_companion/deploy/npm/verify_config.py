#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ipaddress
import re
import urllib.request
from collections import Counter
from pathlib import Path


IPV4_URL = "https://www.cloudflare.com/ips-v4"
IPV6_URL = "https://www.cloudflare.com/ips-v6"
DOMAIN = "comma.danielv.no"


def fail(message: str) -> None:
  raise SystemExit(f"NPM config verification failed: {message}")


def normalized(value: str) -> str:
  return re.sub(r"\s+", " ", value.strip())


def strip_comments(text: str) -> str:
  output: list[str] = []
  quote: str | None = None
  escaped = False
  comment = False
  for character in text:
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
  if quote is not None:
    fail("template contains an unterminated quoted string")
  return "".join(output)


def closing_brace(text: str, opening: int) -> int:
  depth = 1
  quote: str | None = None
  escaped = False
  for index in range(opening + 1, len(text)):
    character = text[index]
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
        return index
  fail("template contains an unclosed block")
  raise AssertionError


def direct_items(text: str) -> tuple[list[str], list[tuple[str, str]]]:
  statements: list[str] = []
  blocks: list[tuple[str, str]] = []
  start = 0
  quote: str | None = None
  escaped = False
  index = 0
  while index < len(text):
    character = text[index]
    if escaped:
      escaped = False
      index += 1
      continue
    if character == "\\" and quote is not None:
      escaped = True
      index += 1
      continue
    if character in {"'", '"'}:
      if quote == character:
        quote = None
      elif quote is None:
        quote = character
      index += 1
      continue
    if quote is None and character == ";":
      statement = normalized(text[start:index])
      if statement:
        statements.append(statement)
      start = index + 1
    elif quote is None and character == "{":
      header = normalized(text[start:index])
      if not header:
        fail("template contains a block without a header")
      end = closing_brace(text, index)
      blocks.append((header, text[index + 1:end]))
      start = end + 1
      index = end
    elif quote is None and character == "}":
      fail("template contains an unmatched closing brace")
    index += 1
  if normalized(text[start:]):
    fail(f"template has unterminated content: {normalized(text[start:])!r}")
  return statements, blocks


def exact_counter(
  actual: list[str],
  expected: set[str],
  label: str,
) -> None:
  counts = Counter(actual)
  duplicate = sorted(item for item, count in counts.items() if count != 1)
  if duplicate:
    fail(f"{label} contains duplicate statements: {duplicate}")
  if set(actual) != expected:
    missing = sorted(expected - set(actual))
    extra = sorted(set(actual) - expected)
    fail(f"{label} statements differ; missing={missing}, extra={extra}")


def unique_blocks(
  blocks: list[tuple[str, str]],
  expected_headers: set[str],
  label: str,
) -> dict[str, str]:
  counts = Counter(header for header, _ in blocks)
  duplicate = sorted(header for header, count in counts.items() if count != 1)
  if duplicate:
    fail(f"{label} contains duplicate blocks: {duplicate}")
  if set(counts) != expected_headers:
    missing = sorted(expected_headers - set(counts))
    extra = sorted(set(counts) - expected_headers)
    fail(f"{label} blocks differ; missing={missing}, extra={extra}")
  return dict(blocks)


def official_ranges() -> set[str]:
  values: set[str] = set()
  for url in (IPV4_URL, IPV6_URL):
    try:
      request = urllib.request.Request(
        url,
        headers={"User-Agent": "comma-companion-deployment-verifier/1"},
      )
      with urllib.request.urlopen(request, timeout=15) as response:
        text = response.read().decode("ascii")
    except Exception as error:
      fail(f"cannot fetch {url}: {type(error).__name__}: {error}")
    for line in text.splitlines():
      value = line.strip()
      if value:
        try:
          values.add(str(ipaddress.ip_network(value, strict=True)))
        except ValueError as error:
          fail(f"{url} returned an invalid network {value!r}: {error}")
  if len([value for value in values if ":" not in value]) != 15:
    fail("Cloudflare IPv4 source did not contain exactly 15 ranges")
  if len([value for value in values if ":" in value]) != 7:
    fail("Cloudflare IPv6 source did not contain exactly 7 ranges")
  return values


def closed_realip_ranges(text: str) -> set[str]:
  networks: set[str] = set()
  header_count = 0
  recursive_count = 0
  for line in strip_comments(text).splitlines():
    line = normalized(line)
    if not line:
      continue
    match = re.fullmatch(r"set_real_ip_from ([0-9A-Fa-f:./]+);", line)
    if match:
      try:
        network = str(ipaddress.ip_network(match.group(1), strict=True))
      except ValueError as error:
        fail(f"invalid trusted Cloudflare range {match.group(1)!r}: {error}")
      if network in networks:
        fail(f"duplicate trusted Cloudflare range: {network}")
      networks.add(network)
    elif line == "real_ip_header CF-Connecting-IP;":
      header_count += 1
    elif line == "real_ip_recursive off;":
      recursive_count += 1
    else:
      fail(f"unexpected active real-IP directive: {line}")
  if header_count != 1 or recursive_count != 1:
    fail("real-IP header and recursive mode must each appear exactly once")
  return networks


def closed_geo_ranges(text: str) -> set[str]:
  networks: set[str] = set()
  for line in strip_comments(text).splitlines():
    line = normalized(line)
    if not line:
      continue
    match = re.fullmatch(r"([0-9A-Fa-f:./]+) 1;", line)
    if not match:
      fail(f"unexpected active Cloudflare geo directive: {line}")
    try:
      network = str(ipaddress.ip_network(match.group(1), strict=True))
    except ValueError as error:
      fail(f"invalid gated Cloudflare range {match.group(1)!r}: {error}")
    if network in networks:
      fail(f"duplicate gated Cloudflare range: {network}")
    networks.add(network)
  return networks


COMMON_PROXY = {
  "proxy_pass http://$comma_companion_upstream_host:$comma_companion_upstream_port",
  "proxy_http_version 1.1",
  "proxy_set_header Host comma.danielv.no",
  "proxy_set_header X-Forwarded-Host comma.danielv.no",
  "proxy_set_header X-Forwarded-Port 443",
  "proxy_set_header X-Forwarded-Proto https",
  "proxy_set_header X-Forwarded-For $remote_addr",
  "proxy_set_header X-Real-IP $remote_addr",
  'proxy_set_header Forwarded ""',
  'proxy_set_header CF-Connecting-IP ""',
  "proxy_set_header Upgrade $http_upgrade",
  "proxy_set_header Connection $comma_companion_connection_upgrade",
}


def verify_templates(directory: Path) -> None:
  server_raw = (directory / "server.conf").read_text(encoding="utf-8")
  top_raw = (directory / "http-top.conf").read_text(encoding="utf-8")
  realip_raw = (directory / "cloudflare-realip.conf").read_text(encoding="utf-8")
  source_raw = (directory / "cloudflare-source.geo").read_text(encoding="utf-8")
  server_text = strip_comments(server_raw)
  top_text = strip_comments(top_raw)

  if re.search(r"^\s*(?:set_real_ip_from|real_ip_header|real_ip_recursive)\b", server_text, re.M):
    fail("real-IP trust directives must exist only in cloudflare-realip.conf")
  if "Strict-Transport-Security" in server_text:
    fail("HSTS must remain disabled until Full (strict) is proven live")

  top_statements, top_blocks = direct_items(top_text)
  exact_counter(
    top_statements,
    {
      "limit_conn_zone $binary_remote_addr zone=comma_companion_client:10m",
      "limit_req_zone $binary_remote_addr zone=comma_companion_health:1m rate=2r/s",
      "limit_req_zone $binary_remote_addr zone=comma_companion_login:1m rate=5r/m",
      "limit_req_zone $binary_remote_addr zone=comma_companion_mutation:1m rate=1r/s",
      "limit_req_zone $comma_companion_inventory_limit_key zone=comma_companion_inventory:1m rate=1r/s",
    },
    "http-top",
  )
  top_by_header = unique_blocks(
    top_blocks,
    {
      "map $http_upgrade $comma_companion_connection_upgrade",
      "map $http_cf_connecting_ip $comma_companion_cf_header_present",
      "map $request_method $comma_companion_inventory_limit_key",
      "geo $realip_remote_addr $comma_companion_cloudflare_source",
    },
    "http-top",
  )
  exact_counter(
    direct_items(
      top_by_header["map $http_upgrade $comma_companion_connection_upgrade"],
    )[0],
    {"default upgrade", "'' close"},
    "websocket map",
  )
  exact_counter(
    direct_items(
      top_by_header[
        "map $http_cf_connecting_ip $comma_companion_cf_header_present"
      ],
    )[0],
    {"default 1", "'' 0"},
    "Cloudflare header map",
  )
  exact_counter(
    direct_items(
      top_by_header["map $request_method $comma_companion_inventory_limit_key"],
    )[0],
    {'default ""', "POST $binary_remote_addr"},
    "route-inventory method map",
  )
  exact_counter(
    direct_items(
      top_by_header[
        "geo $realip_remote_addr $comma_companion_cloudflare_source"
      ],
    )[0],
    {
      "default 0",
      "include /data/nginx/custom/comma-companion/current/cloudflare-source.geo",
    },
    "Cloudflare source geo",
  )
  if any(direct_items(body)[1] for body in top_by_header.values()):
    fail("http-top map/geo blocks must not contain nested blocks")

  outer_statements, outer_blocks = direct_items(server_text)
  if outer_statements:
    fail(f"server template has content outside its server block: {outer_statements}")
  server_map = unique_blocks(outer_blocks, {"server"}, "server template")
  server_statements, server_blocks = direct_items(server_map["server"])
  exact_counter(
    server_statements,
    {
      "listen 443 ssl",
      "listen [::]:443 ssl",
      "http2 on",
      f"server_name {DOMAIN}",
      'set $comma_companion_upstream_host "comma-companion"',
      "set $comma_companion_upstream_port 8000",
      "resolver 127.0.0.11 valid=10s ipv6=off",
      "resolver_timeout 5s",
      "ssl_certificate /data/nginx/custom/comma-companion/current/tls/origin.pem",
      "ssl_certificate_key /data/nginx/custom/comma-companion/current/tls/origin.key",
      "ssl_protocols TLSv1.2 TLSv1.3",
      "ssl_session_tickets off",
      "server_tokens off",
      "include /data/nginx/custom/comma-companion/current/cloudflare-realip.conf",
      "access_log /data/logs/comma-companion_access.log combined",
      "error_log /data/logs/comma-companion_error.log warn",
      "client_max_body_size 2m",
      "client_body_timeout 30s",
      "proxy_request_buffering on",
      "proxy_max_temp_file_size 0",
      "proxy_connect_timeout 30s",
      "proxy_send_timeout 30s",
      "proxy_read_timeout 30s",
      "limit_conn_status 429",
      "limit_req_status 429",
      "limit_conn comma_companion_client 20",
    },
    "server",
  )

  location_headers = {
    "location = /api/v1/readiness",
    "location = /api/v1/health",
    "location = /api/v1/auth/login",
    "location = /api/v1/devices",
    "location = /api/v1/route-inventories",
    "location ~ ^/api/v1/devices/[A-Za-z0-9._-]+/commands$",
    'location ~ "^/api/v1/uploads/[0-9a-f]{32}$"',
    "location /",
  }
  block_map = unique_blocks(
    server_blocks,
    {
      "if ($comma_companion_cloudflare_source = 0)",
      "if ($comma_companion_cf_header_present = 0)",
      *location_headers,
    },
    "server",
  )
  for gate in (
    "if ($comma_companion_cloudflare_source = 0)",
    "if ($comma_companion_cf_header_present = 0)",
  ):
    statements, nested = direct_items(block_map[gate])
    exact_counter(statements, {"return 403"}, gate)
    if nested:
      fail(f"{gate} must not contain a nested block")

  readiness_statements, readiness_blocks = direct_items(
    block_map["location = /api/v1/readiness"],
  )
  exact_counter(
    readiness_statements,
    {"return 404"},
    "public readiness location",
  )
  if readiness_blocks:
    fail("public readiness location must not contain nested blocks")

  location_extras = {
    "location = /api/v1/health": {
      "limit_req zone=comma_companion_health burst=4 nodelay",
      "add_header X-Comma-Client-IP $remote_addr always",
      "proxy_connect_timeout 2s",
      "proxy_send_timeout 5s",
      "proxy_read_timeout 5s",
    },
    "location = /api/v1/auth/login": {
      "limit_req zone=comma_companion_login burst=3 nodelay",
    },
    "location = /api/v1/devices": {
      "limit_req zone=comma_companion_mutation burst=5 nodelay",
    },
    "location = /api/v1/route-inventories": {
      "limit_req zone=comma_companion_inventory burst=10 nodelay",
    },
    "location ~ ^/api/v1/devices/[A-Za-z0-9._-]+/commands$": {
      "limit_req zone=comma_companion_mutation burst=3 nodelay",
    },
    "location /": set(),
  }
  for header, extras in location_extras.items():
    statements, nested = direct_items(block_map[header])
    exact_counter(statements, COMMON_PROXY | extras, header)
    if nested:
      fail(f"{header} must not contain nested blocks")

  upload_header = 'location ~ "^/api/v1/uploads/[0-9a-f]{32}$"'
  upload_statements, upload_blocks = direct_items(block_map[upload_header])
  exact_counter(
    upload_statements,
    COMMON_PROXY
    | {
      "limit_conn comma_companion_client 4",
      "client_max_body_size 20m",
      "client_body_timeout 10m",
      "proxy_send_timeout 10m",
      "proxy_read_timeout 10m",
      "proxy_request_buffering off",
    },
    upload_header,
  )
  upload_nested = unique_blocks(
    upload_blocks,
    {"limit_except GET HEAD PATCH"},
    upload_header,
  )
  denied_statements, denied_blocks = direct_items(
    upload_nested["limit_except GET HEAD PATCH"],
  )
  exact_counter(denied_statements, {"deny all"}, "upload method restriction")
  if denied_blocks:
    fail("upload method restriction must not contain nested blocks")

  official = official_ranges()
  trusted = closed_realip_ranges(realip_raw)
  gated = closed_geo_ranges(source_raw)
  if trusted != official:
    fail("real-IP trust differs from Cloudflare's current published ranges")
  if gated != official:
    fail("source gate differs from Cloudflare's current published ranges")
    print("NPM template verification passed: structural policy and 15 IPv4 + 7 IPv6 Cloudflare ranges")


def main() -> int:
  parser = argparse.ArgumentParser(
    description="Verify the NPM server policy and current Cloudflare ranges.",
  )
  parser.add_argument(
    "config_directory",
    type=Path,
    nargs="?",
    default=Path(__file__).parent,
  )
  arguments = parser.parse_args()
  verify_templates(arguments.config_directory)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
