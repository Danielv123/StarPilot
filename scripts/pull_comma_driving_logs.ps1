param(
  [string]$HostName = "10.30.1.75",
  [string]$User = "comma",
  [string]$RemoteRoot = "/data/media/0/realdata",
  [string]$DestinationRoot = "D:\comma_driving_logs\10.30.1.75\realdata",
  [int]$Retries = 6,
  [int]$CopyTimeoutSeconds = 900,
  [string]$LogPath = ""
)

$ErrorActionPreference = "Stop"

function Require-Command([string]$Name) {
  $command = Get-Command $Name -ErrorAction SilentlyContinue
  if (-not $command) {
    throw "Required command not found: $Name"
  }
  return $command.Source
}

function Write-Log([string]$Message) {
  $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
  Write-Host $line
  if ($LogPath) {
    Add-Content -LiteralPath $LogPath -Value $line
  }
}

function Get-LocalStats([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) {
    return @{ Count = -1; Bytes = -1 }
  }

  $measure = Get-ChildItem -LiteralPath $Path -Recurse -File -Force -ErrorAction SilentlyContinue |
    Measure-Object -Property Length -Sum
  $sum = $measure.Sum
  if ($null -eq $sum) {
    $sum = 0
  }

  return @{ Count = [int64]$measure.Count; Bytes = [int64]$sum }
}

function Invoke-Remote([string]$Command, [int]$TimeoutSeconds = 15) {
  return & $script:SshPath `
    -o BatchMode=yes `
    -o ConnectTimeout=$TimeoutSeconds `
    -o ServerAliveInterval=15 `
    -o ServerAliveCountMax=4 `
    "$User@$HostName" `
    $Command
}

function Get-RemoteManifest {
  $python = @'
import os
import sys

base = sys.argv[1]
for name in sorted(os.listdir(base)):
  path = os.path.join(base, name)
  if not os.path.isdir(path):
    continue

  count = 0
  total = 0
  for root, dirs, files in os.walk(path):
    for filename in files:
      file_path = os.path.join(root, filename)
      try:
        total += os.path.getsize(file_path)
        count += 1
      except OSError:
        pass

  print("{}\t{}\t{}".format(name, count, total))
'@

  $lines = $python | & $script:SshPath `
    -o BatchMode=yes `
    -o ConnectTimeout=20 `
    -o ServerAliveInterval=15 `
    -o ServerAliveCountMax=4 `
    "$User@$HostName" `
    python3 - "$RemoteRoot"

  if ($LASTEXITCODE -ne 0 -or -not $lines) {
    throw "Failed to build remote route manifest from $RemoteRoot"
  }

  $manifest = @()
  foreach ($line in $lines) {
    $parts = $line -split "`t"
    if ($parts.Count -ne 3) {
      continue
    }

    $manifest += [PSCustomObject]@{
      Name = $parts[0]
      Count = [int64]$parts[1]
      Bytes = [int64]$parts[2]
    }
  }

  return $manifest
}

function Join-ScpArgument([string]$Value) {
  if ($Value -notmatch '[\s"]') {
    return $Value
  }

  return '"' + ($Value -replace '\\', '\\' -replace '"', '\"') + '"'
}

function Invoke-ScpRoute([string]$RouteName) {
  $source = "$User@$HostName`:$RemoteRoot/$RouteName"
  $target = "$DestinationRoot\"
  $arguments = @(
    "-r",
    "-p",
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
    $source,
    $target
  )

  $process = New-Object System.Diagnostics.Process
  $process.StartInfo.FileName = $script:ScpPath
  $process.StartInfo.Arguments = ($arguments | ForEach-Object { Join-ScpArgument $_ }) -join " "
  $process.StartInfo.UseShellExecute = $false
  $process.StartInfo.CreateNoWindow = $true

  [void]$process.Start()
  $finished = $process.WaitForExit($CopyTimeoutSeconds * 1000)
  if (-not $finished) {
    try {
      $process.Kill()
    } catch {
    }
    return 124
  }

  return $process.ExitCode
}

$script:SshPath = Require-Command ssh
$script:ScpPath = Require-Command scp

New-Item -ItemType Directory -Force -Path $DestinationRoot | Out-Null
$destinationFullPath = [System.IO.Path]::GetFullPath($DestinationRoot)

if (-not $LogPath) {
  $logDirectory = Split-Path -Parent $DestinationRoot
  if (-not $logDirectory) {
    $logDirectory = "."
  }
  $LogPath = Join-Path $logDirectory "pull_comma_driving_logs.log"
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null

Write-Log "Checking SSH connection to $User@$HostName..."
$probe = Invoke-Remote "hostname"
if ($LASTEXITCODE -ne 0) {
  throw "Unable to connect to $User@$HostName"
}
Write-Log "Connected to $($probe -join ' ')"

Write-Log "Building remote manifest for $RemoteRoot..."
$routes = @(Get-RemoteManifest)
if ($routes.Count -eq 0) {
  throw "No route directories found under $RemoteRoot"
}
Write-Log "Remote routes found: $($routes.Count)"

$routes = @(
  $routes | Sort-Object `
    @{ Expression = { Test-Path -LiteralPath (Join-Path $DestinationRoot $_.Name) }; Ascending = $true },
    @{ Expression = "Name"; Ascending = $true }
)

$copied = 0
$skipped = 0
$routeIndex = 0

foreach ($routeInfo in $routes) {
  $routeIndex++
  $route = $routeInfo.Name
  $localPath = Join-Path $DestinationRoot $route
  $localStats = Get-LocalStats $localPath

  if ($localStats.Count -eq $routeInfo.Count -and $localStats.Bytes -eq $routeInfo.Bytes) {
    Write-Log "[$routeIndex/$($routes.Count)] skip complete $route"
    $skipped++
    continue
  }

  if (Test-Path -LiteralPath $localPath) {
    $localFullPath = [System.IO.Path]::GetFullPath($localPath)
    if (-not $localFullPath.StartsWith($destinationFullPath, [System.StringComparison]::OrdinalIgnoreCase)) {
      throw "Refusing to remove unexpected path: $localFullPath"
    }

    Write-Log "[$routeIndex/$($routes.Count)] removing incomplete $route (local $($localStats.Count)/$($localStats.Bytes), remote $($routeInfo.Count)/$($routeInfo.Bytes))"
    Remove-Item -LiteralPath $localPath -Recurse -Force
  } else {
    Write-Log "[$routeIndex/$($routes.Count)] copying $route ($($routeInfo.Count) files, $($routeInfo.Bytes) bytes)"
  }

  $routeCopied = $false
  for ($attempt = 1; $attempt -le $Retries -and -not $routeCopied; $attempt++) {
    $copyStarted = Get-Date
    $scpExitCode = Invoke-ScpRoute $route
    $elapsed = [Math]::Round(((Get-Date) - $copyStarted).TotalSeconds, 1)
    $localStats = Get-LocalStats $localPath

    if ($scpExitCode -eq 0 -and $localStats.Count -eq $routeInfo.Count -and $localStats.Bytes -eq $routeInfo.Bytes) {
      Write-Log "[$routeIndex/$($routes.Count)] copied $route in ${elapsed}s"
      $copied++
      $routeCopied = $true
    } else {
      Write-Log "[$routeIndex/$($routes.Count)] attempt $attempt failed for $route (scp=$scpExitCode, local $($localStats.Count)/$($localStats.Bytes), remote $($routeInfo.Count)/$($routeInfo.Bytes))"
      if (Test-Path -LiteralPath $localPath) {
        Remove-Item -LiteralPath $localPath -Recurse -Force
      }
      Start-Sleep -Seconds ([Math]::Min(45, 10 * $attempt))
    }
  }

  if (-not $routeCopied) {
    throw "Failed to copy $route after $Retries attempts"
  }
}

$finalStats = Get-LocalStats $DestinationRoot
$localRouteNames = @(Get-ChildItem -LiteralPath $DestinationRoot -Directory -Force | ForEach-Object { $_.Name })
$routeCount = $localRouteNames.Count
$remoteRouteNameSet = @{}
foreach ($routeInfo in $routes) {
  $remoteRouteNameSet[$routeInfo.Name] = $true
}
$localOnlyRouteCount = @($localRouteNames | Where-Object { -not $remoteRouteNameSet.ContainsKey($_) }).Count
$gib = [Math]::Round($finalStats.Bytes / 1GB, 2)

Write-Log ""
Write-Log "Done."
Write-Log "Destination: $DestinationRoot"
Write-Log "Remote routes checked: $($routes.Count)"
Write-Log "Local route directories: $routeCount"
Write-Log "Local-only route directories preserved: $localOnlyRouteCount"
Write-Log "Files: $($finalStats.Count)"
Write-Log "Size: $($finalStats.Bytes) bytes ($gib GiB)"
Write-Log "Copied this run: $copied"
Write-Log "Skipped complete: $skipped"
Write-Log "Log: $LogPath"
