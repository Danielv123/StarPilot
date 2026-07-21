param(
  [string]$HostName = "100.98.247.60",
  [string]$User = "comma",
  [string]$RemoteRoot = "/data/media/0/realdata",
  [string]$DestinationRoot = "D:\comma_driving_logs\10.30.1.75\realdata",
  [ValidateSet("Logs", "Video", "Both")]
  [string]$Phase = "Both",
  [int]$Retries = 6,
  [int]$CopyTimeoutSeconds = 900,
  [string]$LogPath = ""
)

$ErrorActionPreference = "Stop"

$LogFileNames = @("qlog.zst", "rlog.zst")
$VideoFileNames = @("qcamera.ts", "fcamera.hevc", "ecamera.hevc", "dcamera.hevc")

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

function Join-ScpArgument([string]$Value) {
  if ($Value -notmatch '[\s"]') {
    return $Value
  }

  return '"' + ($Value -replace '\\', '\\' -replace '"', '\"') + '"'
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
for route in sorted(os.listdir(base)):
  route_path = os.path.join(base, route)
  if not os.path.isdir(route_path):
    continue
  for name in sorted(os.listdir(route_path)):
    file_path = os.path.join(route_path, name)
    if not os.path.isfile(file_path):
      continue
    try:
      size = os.path.getsize(file_path)
    except OSError:
      continue
    print("{}\t{}\t{}".format(route, name, size))
'@

  $lines = $python | & $script:SshPath `
    -o BatchMode=yes `
    -o ConnectTimeout=20 `
    -o ServerAliveInterval=15 `
    -o ServerAliveCountMax=4 `
    "$User@$HostName" `
    python3 - "$RemoteRoot"

  if ($LASTEXITCODE -ne 0 -or -not $lines) {
    throw "Failed to build remote file manifest from $RemoteRoot"
  }

  $manifest = @()
  foreach ($line in $lines) {
    $parts = $line -split "`t"
    if ($parts.Count -ne 3) {
      continue
    }

    $manifest += [PSCustomObject]@{
      Route = $parts[0]
      Name = $parts[1]
      Bytes = [int64]$parts[2]
    }
  }

  return $manifest
}

function Select-PhaseFiles([array]$Manifest, [string]$SelectedPhase) {
  if ($SelectedPhase -eq "Logs") {
    return @($Manifest | Where-Object { $LogFileNames -contains $_.Name })
  }
  if ($SelectedPhase -eq "Video") {
    return @($Manifest | Where-Object { $VideoFileNames -contains $_.Name })
  }
  throw "Unknown phase: $SelectedPhase"
}

function Get-LocalFileSize([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
    return -1
  }
  return [int64](Get-Item -LiteralPath $Path).Length
}

function Invoke-ScpFile([string]$RouteName, [string]$FileName, [string]$TargetDirectory) {
  $source = "$User@$HostName`:$RemoteRoot/$RouteName/$FileName"
  $arguments = @(
    "-p",
    "-o", "ConnectTimeout=15",
    "-o", "ServerAliveInterval=15",
    "-o", "ServerAliveCountMax=4",
    $source,
    "$TargetDirectory\"
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

function Copy-Phase([string]$SelectedPhase, [array]$Manifest) {
  $files = @(
    Select-PhaseFiles $Manifest $SelectedPhase |
      Sort-Object `
        @{ Expression = { $_.Route -replace '--\d+$', '' }; Descending = $true },
        @{ Expression = { if ($_.Route -match '--(\d+)$') { [int]$Matches[1] } else { -1 } }; Descending = $true },
        @{ Expression = { $_.Name }; Ascending = $true }
  )
  Write-Log "$SelectedPhase phase files found: $($files.Count)"

  $copied = 0
  $skipped = 0
  $failed = 0
  $index = 0

  foreach ($file in $files) {
    $index++
    $routePath = Join-Path $DestinationRoot $file.Route
    $localPath = Join-Path $routePath $file.Name
    $localSize = Get-LocalFileSize $localPath

    if ($localSize -eq $file.Bytes) {
      Write-Log "[$SelectedPhase $index/$($files.Count)] skip complete $($file.Route)/$($file.Name)"
      $skipped++
      continue
    }

    New-Item -ItemType Directory -Force -Path $routePath | Out-Null
    if ($localSize -ge 0) {
      Write-Log "[$SelectedPhase $index/$($files.Count)] replacing incomplete $($file.Route)/$($file.Name) (local $localSize, remote $($file.Bytes))"
      Remove-Item -LiteralPath $localPath -Force
    } else {
      Write-Log "[$SelectedPhase $index/$($files.Count)] copying $($file.Route)/$($file.Name) ($($file.Bytes) bytes)"
    }

    $fileCopied = $false
    for ($attempt = 1; $attempt -le $Retries -and -not $fileCopied; $attempt++) {
      $copyStarted = Get-Date
      $scpExitCode = Invoke-ScpFile $file.Route $file.Name $routePath
      $elapsed = [Math]::Round(((Get-Date) - $copyStarted).TotalSeconds, 1)
      $localSize = Get-LocalFileSize $localPath

      if ($scpExitCode -eq 0 -and $localSize -eq $file.Bytes) {
        Write-Log "[$SelectedPhase $index/$($files.Count)] copied $($file.Route)/$($file.Name) in ${elapsed}s"
        $copied++
        $fileCopied = $true
      } else {
        Write-Log "[$SelectedPhase $index/$($files.Count)] attempt $attempt failed for $($file.Route)/$($file.Name) (scp=$scpExitCode, local $localSize, remote $($file.Bytes))"
        if (Test-Path -LiteralPath $localPath -PathType Leaf) {
          Remove-Item -LiteralPath $localPath -Force
        }
        Start-Sleep -Seconds ([Math]::Min(45, 10 * $attempt))
      }
    }

    if (-not $fileCopied) {
      $failed++
      throw "Failed to copy $($file.Route)/$($file.Name) after $Retries attempts"
    }
  }

  Write-Log "$SelectedPhase phase complete: copied=$copied skipped=$skipped failed=$failed"
}

$script:SshPath = Require-Command ssh
$script:ScpPath = Require-Command scp

New-Item -ItemType Directory -Force -Path $DestinationRoot | Out-Null

if (-not $LogPath) {
  $logDirectory = Split-Path -Parent $DestinationRoot
  if (-not $logDirectory) {
    $logDirectory = "."
  }
  $LogPath = Join-Path $logDirectory "pull_comma_driving_data_phased.log"
}
New-Item -ItemType Directory -Force -Path (Split-Path -Parent $LogPath) | Out-Null

Write-Log "Checking SSH connection to $User@$HostName..."
$probe = Invoke-Remote "hostname"
if ($LASTEXITCODE -ne 0) {
  throw "Unable to connect to $User@$HostName"
}
Write-Log "Connected to $($probe -join ' ')"

Write-Log "Building remote file manifest for $RemoteRoot..."
$manifest = @(Get-RemoteManifest)
$routeCount = @($manifest | Select-Object -ExpandProperty Route -Unique).Count
Write-Log "Remote routes found: $routeCount"
Write-Log "Remote files found: $($manifest.Count)"

if ($Phase -eq "Both") {
  Copy-Phase "Logs" $manifest
  Copy-Phase "Video" $manifest
} else {
  Copy-Phase $Phase $manifest
}

Write-Log "Done."
Write-Log "Destination: $DestinationRoot"
Write-Log "Log: $LogPath"
