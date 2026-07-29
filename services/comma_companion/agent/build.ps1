param(
  [string]$Version = "0.1.0"
)

$ErrorActionPreference = "Stop"
$agentRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$requiredGoVersion = [version]"1.26.5"
$govulncheckVersion = "v1.6.0"
$actualGoVersionText = (& go -C $agentRoot env GOVERSION).Trim()
if ($actualGoVersionText -notmatch '^go(?<version>\d+\.\d+\.\d+)$') {
  throw "Unable to parse Go toolchain version: $actualGoVersionText"
}
$actualGoVersion = [version]$Matches.version
if ($actualGoVersion -ne $requiredGoVersion) {
  throw "The reviewed Go $requiredGoVersion toolchain is required; got $actualGoVersion"
}
$distDir = Join-Path $agentRoot "dist"
$binaryPath = Join-Path $distDir "comma-companion-agent-linux-arm64"
$helperBinaryPath = Join-Path $distDir "comma-companion-control-helper-linux-arm64"

Push-Location $agentRoot
try {
  Write-Host "Using $actualGoVersionText"
  go run "golang.org/x/vuln/cmd/govulncheck@$govulncheckVersion" ./...
  go test ./...
  New-Item -ItemType Directory -Force -Path $distDir | Out-Null
  $previousGoos = $env:GOOS
  $previousGoarch = $env:GOARCH
  $previousCgo = $env:CGO_ENABLED
  try {
    $env:GOOS = "linux"
    $env:GOARCH = "arm64"
    $env:CGO_ENABLED = "0"
    go build -trimpath -ldflags "-s -w -X main.version=$Version" -o $binaryPath ./cmd/agent
    go build -trimpath -ldflags "-s -w -X main.version=$Version" -o $helperBinaryPath ./cmd/control-helper
  } finally {
    $env:GOOS = $previousGoos
    $env:GOARCH = $previousGoarch
    $env:CGO_ENABLED = $previousCgo
  }
  Write-Host "Built $binaryPath"
  Write-Host "Built $helperBinaryPath (staged only; not installed or enabled)"
} finally {
  Pop-Location
}
