[CmdletBinding()]
param(
    [Parameter()]
    [ValidateSet('chrome', 'chromium', 'edge', 'comet')]
    [string]$Browser,

    [Parameter()]
    [string]$BrowserPath
)

$ErrorActionPreference = 'Stop'
$installerArguments = @{}
if ($PSBoundParameters.ContainsKey('Browser')) {
    $installerArguments.Browser = $Browser
}
if ($PSBoundParameters.ContainsKey('BrowserPath')) {
    $installerArguments.BrowserPath = $BrowserPath
}
& (Join-Path $PSScriptRoot 'ollama-comet\Install-OllamaComet.ps1') @installerArguments
