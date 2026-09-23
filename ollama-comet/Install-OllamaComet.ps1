[CmdletBinding()]
param(
    [Parameter()]
    [ValidateSet('chrome', 'chromium', 'edge', 'comet')]
    [string]$Browser,

    [Parameter()]
    [string]$BrowserPath
)

$ErrorActionPreference = 'Stop'

$sourceRoot = $PSScriptRoot
$installRoot = Join-Path $env:LOCALAPPDATA 'AutonomousBrowserAutomation\bin'
$requiredFiles = @('bridge.py', 'Launch-AutonomousBrowser.ps1', 'Launch-OllamaComet.ps1', 'ollama.cmd')
$pythonPath = 'C:\Python314\python.exe'
$runtimeRoot = Join-Path $env:LOCALAPPDATA 'OllamaComet'
$runtimePidPath = Join-Path $runtimeRoot 'bridge.pid'
$runtimeTokenPath = Join-Path $runtimeRoot 'bridge.token'
$browserPathsPath = Join-Path (Split-Path -Parent $installRoot) 'browser-paths.json'

function Resolve-ConfiguredBrowserPath {
    param(
        [Parameter(Mandatory)] [string]$Name,
        [Parameter(Mandatory)] [string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        throw "The configured browser path does not exist: $Path"
    }
    $item = Get-Item -LiteralPath $Path
    if (-not $item.PSIsContainer) {
        if ($item.Extension -ne '.exe') {
            throw "The configured browser path must be an .exe file or installation folder: $Path"
        }
        return $item.FullName
    }

    $relativeCandidates = switch ($Name) {
        'chrome' { @('chrome.exe', 'Application\chrome.exe') }
        'chromium' { @('chrome.exe', 'chromium.exe', 'Application\chrome.exe') }
        'edge' { @('msedge.exe', 'Application\msedge.exe') }
        'comet' { @('comet.exe', 'Application\comet.exe') }
    }
    foreach ($relativePath in $relativeCandidates) {
        $candidate = Join-Path $item.FullName $relativePath
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            return (Get-Item -LiteralPath $candidate).FullName
        }
    }
    throw "No $Name executable was found under $($item.FullName)."
}

function Save-BrowserPath {
    param(
        [Parameter(Mandatory)] [string]$Name,
        [Parameter(Mandatory)] [string]$Path
    )

    $settings = if (Test-Path -LiteralPath $browserPathsPath) {
        Get-Content -Raw -LiteralPath $browserPathsPath | ConvertFrom-Json
    }
    else {
        [pscustomobject]@{}
    }
    $settings | Add-Member -NotePropertyName $Name -NotePropertyValue $Path -Force
    $settings | ConvertTo-Json | Set-Content -LiteralPath $browserPathsPath -Encoding UTF8
}

function Install-PowerShellShim {
    $documents = [Environment]::GetFolderPath('MyDocuments')
    $profilePaths = @(
        (Join-Path $documents 'WindowsPowerShell\profile.ps1'),
        (Join-Path $documents 'PowerShell\profile.ps1')
    )
    $startMarker = '# >>> AutonomousBrowserAutomation >>>'
    $endMarker = '# <<< AutonomousBrowserAutomation <<<'
    $block = @'
# >>> AutonomousBrowserAutomation >>>
function global:ollama {
    & "$env:LOCALAPPDATA\AutonomousBrowserAutomation\bin\ollama.cmd" @args
}
# <<< AutonomousBrowserAutomation <<<
'@

    foreach ($profilePath in $profilePaths) {
        $profileDirectory = Split-Path -Parent $profilePath
        if (-not (Test-Path $profileDirectory)) {
            New-Item -ItemType Directory -Path $profileDirectory -Force | Out-Null
        }
        $content = if (Test-Path $profilePath) {
            Get-Content -Raw $profilePath
        }
        else {
            ''
        }
        $pattern = "(?ms)^$([regex]::Escape($startMarker)).*?^$([regex]::Escape($endMarker))\s*"
        $content = [regex]::Replace($content, $pattern, '').TrimEnd()
        $updated = if ($content) { "$content`r`n`r`n$block`r`n" } else { "$block`r`n" }
        Set-Content -LiteralPath $profilePath -Value $updated -Encoding UTF8
    }
}

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

if ($BrowserPath -and -not $Browser) {
    throw '-Browser is required when -BrowserPath is provided.'
}
if ($Browser) {
    if (-not $BrowserPath) {
        throw '-BrowserPath is required when -Browser is provided.'
    }
    $configuredBrowserPath = Resolve-ConfiguredBrowserPath -Name $Browser -Path $BrowserPath
    Save-BrowserPath -Name $Browser -Path $configuredBrowserPath
    Write-Host "Configured $Browser executable: $configuredBrowserPath" -ForegroundColor Green
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
Install-PowerShellShim

Write-Host "Installed Autonomous Browser Automation to $installRoot" -ForegroundColor Green
Write-Host 'Open a new terminal, then run:'
Write-Host '  ollama launch chrome'
Write-Host '  ollama launch chromium --bridge-only'
Write-Host '  ollama launch edge'
Write-Host '  ollama launch comet'
Write-Host 'To save a specific browser executable or installation folder, reinstall with:'
Write-Host '  .\Install-AutonomousBrowserAutomation.ps1 -Browser chromium -BrowserPath "C:\path\to\Chromium"'
Write-Host 'Configure direct Ollama Cloud access with:'
Write-Host '  ollama launch chrome --config'
Write-Host 'For an already-open PowerShell window, refresh the command with:'
Write-Host '  . $PROFILE.CurrentUserAllHosts'
