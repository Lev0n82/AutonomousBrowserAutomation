[CmdletBinding()]
param(
    [string]$Version = '0.2.0'
)

$ErrorActionPreference = 'Stop'
$extensionRoot = Split-Path -Parent $PSScriptRoot
$projectRoot = Split-Path -Parent $extensionRoot
$artifacts = Join-Path $projectRoot 'artifacts'

& (Join-Path $PSScriptRoot 'build.ps1') -Target all

if (-not (Test-Path $artifacts)) {
    New-Item -ItemType Directory -Path $artifacts -Force | Out-Null
}

$packages = @{
    chromium = "AutonomousBrowserAutomation-Chromium-v$Version.zip"
    firefox = "AutonomousBrowserAutomation-Firefox-v$Version.zip"
}

foreach ($browser in $packages.Keys) {
    $archive = Join-Path $artifacts $packages[$browser]
    Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
    Compress-Archive -Path (Join-Path $extensionRoot "dist\$browser\*") `
        -DestinationPath $archive -CompressionLevel Optimal
    Write-Host "Created $archive"
}
