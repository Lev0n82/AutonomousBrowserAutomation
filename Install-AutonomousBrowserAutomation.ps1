[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'ollama-comet\Install-OllamaComet.ps1')
