[CmdletBinding()]
param(
    [Parameter()]
    [string]$Model,

    [Parameter()]
    [switch]$Config,

    [Parameter()]
    [switch]$Restore,

    [Parameter(ValueFromRemainingArguments)]
    [string[]]$ExtraArguments
)

$ErrorActionPreference = 'Stop'

$appRoot = Join-Path $env:LOCALAPPDATA 'OllamaComet'
$configPath = Join-Path $appRoot 'config.json'
$credentialPath = Join-Path $appRoot 'cloud-key.clixml'
$pidPath = Join-Path $appRoot 'bridge.pid'
$tokenPath = Join-Path $appRoot 'bridge.token'
$logPath = Join-Path $appRoot 'bridge.log'
$errorLogPath = Join-Path $appRoot 'bridge-error.log'
$profilePath = Join-Path $appRoot 'Comet Profile'
$bridgeScript = Join-Path $PSScriptRoot 'bridge.py'
$pythonPath = 'C:\Python314\python.exe'
$cometPath = 'C:\Program Files\Perplexity\Comet\Application\comet.exe'
$port = 11435
$debugPort = 9223

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
    if (-not (Test-Path $pidPath)) {
        return
    }

    $bridgeProcessId = 0
    if ([int]::TryParse((Get-Content -Raw $pidPath).Trim(), [ref]$bridgeProcessId)) {
        $process = Get-Process -Id $bridgeProcessId -ErrorAction SilentlyContinue
        if ($null -ne $process) {
            Stop-Process -Id $bridgeProcessId -Force
            $process.WaitForExit(5000)
        }
    }
    Remove-Item -LiteralPath $pidPath -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tokenPath -Force -ErrorAction SilentlyContinue
}

function Configure-Integration {
    $current = Read-Configuration
    Write-Host ''
    Write-Host 'Configure Ollama for Comet' -ForegroundColor Cyan
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
    Write-Host 'Ollama Comet configuration was restored to local defaults.'
    return
}

if ($Config) {
    Configure-Integration
    return
}

if (-not (Test-Path $pythonPath)) {
    throw "Python was not found at $pythonPath."
}
if (-not (Test-Path $cometPath)) {
    throw "Comet was not found at $cometPath."
}
if (-not (Test-Path $bridgeScript)) {
    throw "Bridge script was not found at $bridgeScript."
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

    $oldApiKey = $env:OLLAMA_COMET_API_KEY
    try {
        if ($null -ne $apiKey) {
            $env:OLLAMA_COMET_API_KEY = $apiKey
        }
        $process = Start-Process -FilePath $pythonPath `
            -ArgumentList @($bridgeScript, '--port', $port, '--token', $token) `
            -WindowStyle Hidden `
            -RedirectStandardOutput $logPath `
            -RedirectStandardError $errorLogPath `
            -PassThru
        Set-Content -Path $pidPath -Value $process.Id -Encoding ASCII
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
$cometArguments = @(
    "`"--user-data-dir=$profilePath`"",
    "--perplexity-backend-url=http://127.0.0.1:$port",
    "--remote-debugging-port=$debugPort",
    '--remote-debugging-address=127.0.0.1',
    '--remote-allow-origins=http://127.0.0.1',
    '--no-first-run',
    '--new-window',
    $assistantUrl
)
if ($ExtraArguments) {
    $cometArguments += $ExtraArguments
}

Start-Process -FilePath $cometPath -ArgumentList $cometArguments | Out-Null
Write-Host "Comet launched with Ollama model '$($configuration.model)'." -ForegroundColor Green
Write-Host "Backend: $($configuration.endpoint)"
Write-Host 'The native assistant button opens Ollama; browser actions are enabled in the isolated Comet profile.'
