[CmdletBinding()]
param(
    [ValidateSet('chromium', 'firefox', 'all')]
    [string]$Target = 'all'
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$targets = if ($Target -eq 'all') { @('chromium', 'firefox') } else { @($Target) }

foreach ($browser in $targets) {
    $destination = Join-Path $root "dist\$browser"
    if (Test-Path $destination) {
        Remove-Item -LiteralPath $destination -Recurse -Force
    }
    New-Item -ItemType Directory -Path $destination -Force | Out-Null
    Copy-Item -Path (Join-Path $root 'src\*') -Destination $destination -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $root "manifests\$browser.json") `
        -Destination (Join-Path $destination 'manifest.json') -Force
    Write-Host "Built $browser extension at $destination"
}
