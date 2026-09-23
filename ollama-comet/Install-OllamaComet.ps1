[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$sourceRoot = $PSScriptRoot
$installRoot = Join-Path $env:LOCALAPPDATA 'AutonomousBrowserAutomation\bin'
$requiredFiles = @('bridge.py', 'Launch-AutonomousBrowser.ps1', 'Launch-OllamaComet.ps1', 'ollama.cmd')
$pythonPath = 'C:\Python314\python.exe'
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'OllamaComet'
$runtimePidPath = Join-Path $runtimeRoot 'bridge.pid'
$runtimeTokenPath = Join-Path $runtimeRoot 'bridge.token'

if (Test-Path $runtimePidPath) {
    $runningProcessId = 0
    if ([int]::TryParse((Get-Content -Raw $runtimePidPath).Trim(), [ref]$runningProcessId)) {
        $runningProcess = Get-Process -Id $runningProcessId -ErrorAction SilentlyContinue
        if ($null -ne $runningProcess) {
            Stop-Process -Id $runningProcessId -Force
            $runningProcess.WaitForExit(5000)
        }
    }
    Remove-Item -LiteralPath $runtimePidPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $runtimeTokenPath -Force -ErrorAction SilentlyContinue
}

if (-not (Test-Path $installRoot)) {
    New-Item -ItemType Directory -Path $installRoot -Force | Out-Null
}

foreach ($file in $requiredFiles) {
    Copy-Item -LiteralPath (Join-Path $sourceRoot $file) -Destination (Join-Path $installRoot $file) -Force
}

$projectRoot = Split-Path -Parent $sourceRoot
$buildScript = Join-Path $projectRoot 'extension\scripts\build.ps1'
if (-not (Test-Path $buildScript)) {
    throw "Extension build script was not found at $buildScript"
}
& $buildScript -Target chromium
$extensionSource = Join-Path $projectRoot 'extension\dist\chromium'
$extensionDestination = Join-Path $installRoot 'browser-extension'
if (-not (Test-Path (Join-Path $extensionSource 'manifest.json'))) {
    throw "Chromium extension build was not found at $extensionSource"
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

Write-Host "Installed Autonomous Browser Automation to $installRoot" -ForegroundColor Green
Write-Host 'Open a new terminal, then run:'
Write-Host '  ollama launch chrome'
Write-Host '  ollama launch edge'
Write-Host '  ollama launch comet'
Write-Host 'Configure direct Ollama Cloud access with:'
Write-Host '  ollama launch chrome --config'
