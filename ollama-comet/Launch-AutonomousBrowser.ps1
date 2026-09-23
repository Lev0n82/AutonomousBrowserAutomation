[CmdletBinding()]
param(
    [Parameter()]
    [ValidateSet('chrome', 'chromium', 'edge', 'comet')]
    [string]$Browser = 'comet',

    [Parameter()]
    [string]$BrowserPath,

    [Parameter()]
    [string]$Model,

    [Parameter()]
    [switch]$Config,

    [Parameter()]
    [switch]$Restore,

    [Parameter()]
    [switch]$Validate,

    [Parameter()]
    [switch]$BridgeOnly,

    [Parameter(ValueFromRemainingArguments)]
    [string[]]$ExtraArguments
)

$ErrorActionPreference = 'Stop'

$appRoot = Join-Path $env:LOCALAPPDATA 'OllamaComet'
$configPath = Join-Path $appRoot 'config.json'
$credentialPath = Join-Path $appRoot 'cloud-key.clixml'
$pidPath = Join-Path $appRoot 'bridge.pid'
$tokenPath = Join-Path $appRoot 'bridge.token'
$targetPath = Join-Path $appRoot 'bridge.target'
$logPath = Join-Path $appRoot 'bridge.log'
$errorLogPath = Join-Path $appRoot 'bridge-error.log'
$browserPathsPath = Join-Path $env:LOCALAPPDATA 'AutonomousBrowserAutomation\browser-paths.json'
$profilePath = Join-Path $appRoot "$Browser Profile"
$bridgeScript = Join-Path $PSScriptRoot 'bridge.py'
$installedExtensionPath = Join-Path $PSScriptRoot 'browser-extension'
$sourceExtensionPath = Join-Path (Split-Path -Parent $PSScriptRoot) 'extension\dist\chromium'
$extensionPath = if (Test-Path (Join-Path $installedExtensionPath 'manifest.json')) {
    $installedExtensionPath
}
else {
    $sourceExtensionPath
}
$pythonPath = 'C:\Python314\python.exe'
$cometPath = 'C:\Program Files\Perplexity\Comet\Application\comet.exe'
$port = 11435
$debugPort = 9223

$normalizedExtraArguments = [System.Collections.Generic.List[string]]::new()
for ($index = 0; $index -lt $ExtraArguments.Count; $index++) {
    $argument = $ExtraArguments[$index]
    switch ($argument.ToLowerInvariant()) {
        '--bridge-only' {
            $BridgeOnly = $true
        }
        '--validate' {
            $Validate = $true
        }
        '--config' {
            $Config = $true
        }
        '--restore' {
            $Restore = $true
        }
        '--model' {
            if ($index + 1 -ge $ExtraArguments.Count) {
                throw '--model requires a model name.'
            }
            $index++
            $Model = $ExtraArguments[$index]
        }
        '--browser-path' {
            if ($index + 1 -ge $ExtraArguments.Count) {
                throw '--browser-path requires an executable path.'
            }
            $index++
            $BrowserPath = $ExtraArguments[$index]
        }
        default {
            $normalizedExtraArguments.Add($argument)
        }
    }
}
$ExtraArguments = $normalizedExtraArguments.ToArray()

function Resolve-BrowserPath {
    param([Parameter(Mandatory)] [string]$Name)

    if (Test-Path -LiteralPath $browserPathsPath) {
        $configuredPaths = Get-Content -Raw -LiteralPath $browserPathsPath | ConvertFrom-Json
        $configuredPath = $configuredPaths.$Name
        if ($configuredPath) {
            if (Test-Path -LiteralPath $configuredPath -PathType Leaf) {
                return (Get-Item -LiteralPath $configuredPath).FullName
            }
            throw "The configured $Name executable no longer exists: $configuredPath. Re-run the installer with -Browser and -BrowserPath."
        }
    }

    $candidates = switch ($Name) {
        'chrome' {
            @(
                (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe'),
                (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe'),
                (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe')
            )
        }
        'chromium' {
            @(
                (Join-Path $env:ProgramFiles 'Chromium\Application\chrome.exe'),
                (Join-Path ${env:ProgramFiles(x86)} 'Chromium\Application\chrome.exe'),
                (Join-Path $env:LOCALAPPDATA 'Chromium\Application\chrome.exe')
            )
        }
        'edge' {
            @(
                (Join-Path ${env:ProgramFiles(x86)} 'Microsoft\Edge\Application\msedge.exe'),
                (Join-Path $env:ProgramFiles 'Microsoft\Edge\Application\msedge.exe'),
                (Join-Path $env:LOCALAPPDATA 'Microsoft\Edge\Application\msedge.exe')
            )
        }
        'comet' { @($cometPath) }
    }
    $path = $candidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
    if (-not $path) {
        throw "$Name was not found. Install it and run the command again."
    }
    return $path
}

function ConvertFrom-SecureValue {
    param([Security.SecureString]$Value)

    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Value)
    try {
        [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
}

function Read-Configuration {
    if (Test-Path $configPath) {
        $value = Get-Content -Raw $configPath | ConvertFrom-Json
        if (-not ($value.PSObject.Properties.Name -contains 'mode')) {
            $value | Add-Member -NotePropertyName mode -NotePropertyValue 'local'
        }
        if (-not ($value.PSObject.Properties.Name -contains 'local_endpoint')) {
            $value | Add-Member -NotePropertyName local_endpoint -NotePropertyValue 'http://127.0.0.1:11434'
        }
        if (-not ($value.PSObject.Properties.Name -contains 'cloud_endpoint')) {
            $value | Add-Member -NotePropertyName cloud_endpoint -NotePropertyValue 'https://ollama.com'
        }
        if (-not ($value.PSObject.Properties.Name -contains 'local_model')) {
            $localModel = if ($value.mode -eq 'local' -and $value.model) { $value.model } else { 'granite4.1:3b' }
            $value | Add-Member -NotePropertyName local_model -NotePropertyValue $localModel
        }
        if (-not ($value.PSObject.Properties.Name -contains 'cloud_model')) {
            $cloudModel = if ($value.mode -eq 'cloud' -and $value.model) { $value.model } else { '' }
            $value | Add-Member -NotePropertyName cloud_model -NotePropertyValue $cloudModel
        }
        $value.endpoint = if ($value.mode -eq 'cloud') { $value.cloud_endpoint } else { $value.local_endpoint }
        $value.model = if ($value.mode -eq 'cloud') { $value.cloud_model } else { $value.local_model }
        return $value
    }
    [pscustomobject]@{
        mode = 'local'
        local_endpoint = 'http://127.0.0.1:11434'
        cloud_endpoint = 'https://ollama.com'
        local_model = 'granite4.1:3b'
        cloud_model = ''
        endpoint = 'http://127.0.0.1:11434'
        model = 'granite4.1:3b'
    }
}

function Save-Configuration {
    param([Parameter(Mandatory)] $Value)

    if (-not (Test-Path $appRoot)) {
        New-Item -ItemType Directory -Path $appRoot -Force | Out-Null
    }
    $Value | ConvertTo-Json | Set-Content -Path $configPath -Encoding UTF8
}

function Stop-Bridge {
    if (Test-Path $pidPath) {
        $bridgeProcessId = 0
        if ([int]::TryParse((Get-Content -Raw $pidPath).Trim(), [ref]$bridgeProcessId)) {
            $process = Get-Process -Id $bridgeProcessId -ErrorAction SilentlyContinue
            if ($null -ne $process) {
                Stop-Process -Id $bridgeProcessId -Force
                $process.WaitForExit(5000)
            }
        }
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tokenPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $targetPath -Force -ErrorAction SilentlyContinue
}

function Configure-Integration {
    $current = Read-Configuration
    Write-Host ''
    Write-Host 'Configure Ollama for autonomous browser automation' -ForegroundColor Cyan
    Write-Host '1. Local Ollama'
    Write-Host '2. Ollama Cloud API'
    $choice = Read-Host "Mode [1]"
    if ([string]::IsNullOrWhiteSpace($choice)) {
        $choice = '1'
    }

    if ($choice -eq '2') {
        $endpoint = 'https://ollama.com'
        $mode = 'cloud'
        $secureKey = Read-Host 'Ollama Cloud API key' -AsSecureString
        if ($secureKey.Length -eq 0) {
            throw 'An API key is required for direct Ollama Cloud access.'
        }
        if (-not (Test-Path $appRoot)) {
            New-Item -ItemType Directory -Path $appRoot -Force | Out-Null
        }
        $secureKey | Export-Clixml -Path $credentialPath -Force
    }
    else {
        $endpoint = 'http://127.0.0.1:11434'
        $mode = 'local'
    }

    $defaultModel = if ($mode -eq 'cloud') { $current.cloud_model } else { $current.local_model }
    $selectedModel = Read-Host "Model [$defaultModel]"
    if ([string]::IsNullOrWhiteSpace($selectedModel)) {
        $selectedModel = $defaultModel
    }
    $localModel = if ($mode -eq 'local') { $selectedModel } else { $current.local_model }
    $cloudModel = if ($mode -eq 'cloud') { $selectedModel } else { $current.cloud_model }
    Save-Configuration ([pscustomobject]@{
        mode = $mode
        local_endpoint = 'http://127.0.0.1:11434'
        cloud_endpoint = 'https://ollama.com'
        local_model = $localModel
        cloud_model = $cloudModel
        endpoint = $endpoint
        model = $selectedModel
    })
    Stop-Bridge
    Write-Host "Saved configuration to $configPath" -ForegroundColor Green
}

if ($Restore) {
    Stop-Bridge
    if (Test-Path $configPath) {
        Remove-Item -LiteralPath $configPath -Force
    }
    if (Test-Path $credentialPath) {
        Remove-Item -LiteralPath $credentialPath -Force
    }
    if (Test-Path $tokenPath) {
        Remove-Item -LiteralPath $tokenPath -Force
    }
    if (Test-Path $targetPath) {
        Remove-Item -LiteralPath $targetPath -Force
    }
    Write-Host 'Autonomous browser configuration was restored to local defaults.'
    return
}

if ($Config) {
    Configure-Integration
    return
}

if (-not (Test-Path $pythonPath)) {
    throw "Python was not found at $pythonPath."
}
if (-not (Test-Path $bridgeScript)) {
    throw "Bridge script was not found at $bridgeScript."
}
$resolvedBrowserPath = if ($BrowserPath) {
    if (-not (Test-Path $BrowserPath)) {
        throw "The browser executable was not found at $BrowserPath"
    }
    (Resolve-Path $BrowserPath).Path
}
elseif (-not $BridgeOnly) {
    Resolve-BrowserPath $Browser
}
else {
    $null
}
if ($Browser -ne 'comet' -and -not (Test-Path (Join-Path $extensionPath 'manifest.json'))) {
    throw "The browser extension was not found at $extensionPath. Re-run Install-OllamaComet.ps1."
}
$firstBrowserLaunch = $Browser -ne 'comet' -and -not (Test-Path $profilePath)
if ($Validate) {
    Write-Host "Browser: $Browser"
    Write-Host "Executable: $(if ($resolvedBrowserPath) { $resolvedBrowserPath } else { 'not required (bridge only)' })"
    if ($Browser -ne 'comet') {
        Write-Host "Extension: $extensionPath"
    }
    Write-Host 'Launcher validation succeeded.' -ForegroundColor Green
    return
}

$configuration = Read-Configuration
if (-not [string]::IsNullOrWhiteSpace($Model)) {
    $configuration.model = $Model
    if ($configuration.mode -eq 'cloud') {
        $configuration.cloud_model = $Model
    }
    else {
        $configuration.local_model = $Model
    }
    Save-Configuration $configuration
}

$apiKey = $null
if (Test-Path $credentialPath) {
    $apiKey = ConvertFrom-SecureValue (Import-Clixml -Path $credentialPath)
}
elseif ($configuration.mode -eq 'cloud') {
        throw 'Cloud mode is configured but no Windows-protected API key exists. Run: ollama launch comet --config'
}

$runningTarget = if (Test-Path $targetPath) {
    (Get-Content -Raw $targetPath).Trim()
}
else {
    ''
}
if ($runningTarget -and $runningTarget -ne $Browser) {
    Stop-Bridge
}

$bridgeHealthy = $false
try {
    $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 2
    $bridgeHealthy = $health.status -eq 'ok'
}
catch {
    $bridgeHealthy = $false
}

if (-not $bridgeHealthy) {
    if (-not (Test-Path $appRoot)) {
        New-Item -ItemType Directory -Path $appRoot -Force | Out-Null
    }
    $token = if (Test-Path $tokenPath) {
        (Get-Content -Raw $tokenPath).Trim()
    }
    else {
        ''
    }
    if ([string]::IsNullOrWhiteSpace($token)) {
        $tokenBytes = New-Object byte[] 32
        $random = [Security.Cryptography.RandomNumberGenerator]::Create()
        try {
            $random.GetBytes($tokenBytes)
        }
        finally {
            $random.Dispose()
        }
        $token = [Convert]::ToBase64String($tokenBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
        Set-Content -Path $tokenPath -Value $token -Encoding ASCII
    }

    $oldApiKey = $env:OLLAMA_COMET_API_KEY
    try {
        if ($null -ne $apiKey) {
            $env:OLLAMA_COMET_API_KEY = $apiKey
        }
        $process = Start-Process -FilePath $pythonPath `
            -ArgumentList @(
                $bridgeScript,
                '--port', $port,
                '--token', $token,
                '--browser-target', $Browser
            ) `
            -WindowStyle Hidden `
            -RedirectStandardOutput $logPath `
            -RedirectStandardError $errorLogPath `
            -PassThru
        Set-Content -Path $pidPath -Value $process.Id -Encoding ASCII
        Set-Content -Path $targetPath -Value $Browser -Encoding ASCII
    }
    finally {
        $env:OLLAMA_COMET_API_KEY = $oldApiKey
        $apiKey = $null
    }

    $deadline = (Get-Date).AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 200
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$port/health" -TimeoutSec 1
            $bridgeHealthy = $health.status -eq 'ok'
        }
        catch {
            $bridgeHealthy = $false
        }
    } until ($bridgeHealthy -or (Get-Date) -ge $deadline)

    if (-not $bridgeHealthy) {
        throw "The Ollama Comet bridge did not start. See $logPath"
    }
}

if (-not (Test-Path $tokenPath)) {
    throw "The bridge is running without launcher state. Stop process ID $(Get-Content -Raw $pidPath -ErrorAction SilentlyContinue) and launch again."
}

$token = (Get-Content -Raw $tokenPath).Trim()
$assistantUrl = "http://127.0.0.1:$port/sidecar?token=$([Uri]::EscapeDataString($token))"
$browserArguments = if ($Browser -eq 'comet') {
    @(
        "`"--user-data-dir=$profilePath`"",
        "--perplexity-backend-url=http://127.0.0.1:$port",
        "--remote-debugging-port=$debugPort",
        '--remote-debugging-address=127.0.0.1',
        '--remote-allow-origins=http://127.0.0.1',
        '--no-first-run',
        '--new-window',
        $assistantUrl
    )
}
else {
    @{
        bridgeUrl = "http://127.0.0.1:$port"
        token = $token
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $extensionPath 'runtime-config.json') -Encoding UTF8
    @(
        "`"--user-data-dir=$profilePath`"",
        $(if ($Browser -eq 'chromium') { "`"--load-extension=$extensionPath`"" }),
        '--no-first-run',
        '--new-window',
        $(if ($firstBrowserLaunch) {
            if ($Browser -eq 'edge') { 'edge://extensions/' } else { 'chrome://extensions/' }
        }),
        $assistantUrl
    ) | Where-Object { $_ }
}
if ($ExtraArguments) {
    $browserArguments += $ExtraArguments
}

if ($BridgeOnly) {
    Write-Host "Bridge started for $Browser at http://127.0.0.1:$port." -ForegroundColor Green
    Write-Host "Bridge token file: $tokenPath"
    Write-Host 'Open the extension settings in your existing browser and enter the bridge URL and token.'
    return
}

Start-Process -FilePath $resolvedBrowserPath -ArgumentList $browserArguments | Out-Null
Write-Host "$Browser launched with Ollama model '$($configuration.model)'." -ForegroundColor Green
Write-Host "Backend: $($configuration.endpoint)"
if ($Browser -eq 'comet') {
    Write-Host 'The native Comet assistant opens Ollama and uses the signed Comet agent.'
}
else {
    if ($firstBrowserLaunch) {
        Write-Host 'One-time setup is required because official Chrome and Edge do not permit command-line extension installation.' -ForegroundColor Yellow
        Write-Host 'Enable Developer mode, select Load unpacked, and choose:'
        Write-Host "  $extensionPath" -ForegroundColor Cyan
    }
    Write-Host 'Pin Autonomous Browser Assistant and select its toolbar icon to open the side panel.'
}
