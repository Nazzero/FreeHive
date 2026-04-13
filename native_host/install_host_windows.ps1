Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$HostName = "com.freehive.arena_bridge"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ManifestSrc = Join-Path $ScriptDir "$HostName.json"
$TargetDir = Join-Path $env:LOCALAPPDATA "Google\Chrome\User Data\NativeMessagingHosts"
$TargetManifest = Join-Path $TargetDir "$HostName.json"
$HostPy = Join-Path $ScriptDir "host.py"
$HostCmd = Join-Path $ScriptDir "host.cmd"

if (-not (Test-Path $ManifestSrc)) {
  throw "Manifest not found: $ManifestSrc"
}

$cmdContent = @"
@echo off
setlocal
where py >nul 2>&1
if %errorlevel%==0 (
  py -3 "%~dp0host.py"
) else (
  python "%~dp0host.py"
)
"@

Set-Content -Path $HostCmd -Value $cmdContent -Encoding Ascii
New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null

$manifest = Get-Content -Raw -Path $ManifestSrc | ConvertFrom-Json
$manifest.path = $HostCmd
$manifest | ConvertTo-Json -Depth 8 | Set-Content -Path $TargetManifest -Encoding UTF8

Write-Host "Native messaging host '$HostName' installed to $TargetDir"
Write-Host "IMPORTANT: Update 'allowed_origins' in $TargetManifest with your real extension ID."
