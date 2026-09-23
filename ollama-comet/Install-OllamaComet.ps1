[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$sourceRoot = $PSScriptRoot
$installRoot = Join-Path $env:LOCALAPPDATA 'OllamaComet\bin'
$requiredFiles = @('bridge.py', 'Launch-OllamaComet.ps1', 'ollama.cmd')
$pythonPath = 'C:\Python314\python.exe'

if (-not (Test-Path $installRoot)) {
    New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
}

foreach ($file in $requiredFiles) {
    Copy-Item -LiteralPath (Join-Path $sourceRoot $file) -Destination (Join-Path $installRoot $file) -Force
}

$extensionSource = Join-Path $sourceRoot 'browser-control'
$extensionDestination = Join-Path $installRoot 'browser-control'
if (-not (Test-Path (Join-Path $extensionSource 'manifest.json'))) {
    throw "Browser-control extension source was not found at $extensionSource"
}
if (-not (Test-Path $extensionDestination)) {
    New-Item -ItemType Directory -Path $extensionDestination -Force | Out-Null
}
Copy-Item -Path (Join-Path $extensionSource '*') -Destination $extensionDestination -Force

if (-not (Test-Path $pythonPath)) {
    throw "Python was not found at $pythonPath"
}
& $env:ComSpec /c "`"$pythonPath`" -c `"import websocket`" >nul 2>nul"
if ($LASTEXITCODE -ne 0) {
    & $env:ComSpec /c "`"$pythonPath`" -m pip install --quiet websocket-client"
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to install the websocket-client dependency.'
    }
}

& $env:ComSpec /c "`"$pythonPath`" -c `"import pypdf`" >nul 2>nul"
if ($LASTEXITCODE -ne 0) {
    & $env:ComSpec /c "`"$pythonPath`" -m pip install --quiet pypdf"
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to install the pypdf dependency.'
    }
}

$currentUserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$pathEntries = @($currentUserPath -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
$pathEntries = @($pathEntries | Where-Object { $_.TrimEnd('\') -ne $installRoot.TrimEnd('\') })
$newUserPath = (@($installRoot) + $pathEntries) -join ';'
[Environment]::SetEnvironmentVariable('Path', $newUserPath, 'User')
$processPathEntries = @($env:Path -split ';' | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
$processPathEntries = @($processPathEntries | Where-Object { $_.TrimEnd('\') -ne $installRoot.TrimEnd('\') })
$env:Path = (@($installRoot) + $processPathEntries) -join ';'

Write-Host "Installed the Ollama Comet launcher to $installRoot" -ForegroundColor Green
Write-Host 'Open a new terminal, then run:'
Write-Host '  ollama launch comet'
Write-Host 'Configure direct Ollama Cloud access with:'
Write-Host '  ollama launch comet --config'
