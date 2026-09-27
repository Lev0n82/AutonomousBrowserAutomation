[CmdletBinding()]
param(
    [Parameter()]
    [ValidateSet('chrome', 'chromium', 'edge', 'firefox', 'comet')]
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
$installedFirefoxExtensionPath = Join-Path $PSScriptRoot 'browser-extension-firefox'
$extensionDistName = if ($Browser -eq 'firefox') { 'firefox' } else { 'chromium' }
$sourceExtensionPath = Join-Path (Split-Path -Parent $PSScriptRoot) ("extension\dist\$extensionDistName")
# The installed browser-extension copy is a Chromium build; Firefox must use its
# own dist (or an installed browser-extension-firefox copy) instead.
$extensionPath = if ($Browser -eq 'firefox') {
    if (Test-Path (Join-Path $installedFirefoxExtensionPath 'manifest.json')) {
        $installedFirefoxExtensionPath
    }
    else {
        $sourceExtensionPath
    }
}
elseif (Test-Path (Join-Path $installedExtensionPath 'manifest.json')) {
    $installedExtensionPath
}
else {
    $sourceExtensionPath
}
$firefoxExtensionId = 'autonomous-browser-assistant@ollama.local'
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

function Get-ExtensionPolicyVendorKey {
    param([Parameter(Mandatory)] [string]$Browser)

    switch ($Browser) {
        'chrome' { 'Google\Chrome' }
        'edge' { 'Microsoft\Edge' }
        'chromium' { 'Chromium' }
        default { $null }
    }
}

function Get-RegistryValueList {
    param([Parameter(Mandatory)] [string]$Path)

    if (-not (Test-Path -LiteralPath $Path)) {
        return @()
    }
    $property = Get-ItemProperty -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($null -eq $property) {
        return @()
    }
    @($property.PSObject.Properties |
        Where-Object { $_.Name -notmatch '^PS' } |
        ForEach-Object { [string]$_.Value })
}

function Get-UnpackedExtensionId {
    param([Parameter(Mandatory)] [string]$Path)

    $pathValue = if (Test-Path -LiteralPath $Path) {
        (Resolve-Path -LiteralPath $Path).Path
    }
    else {
        $Path
    }
    # Chromium GenerateIdForPath (components/crx_file/id_util.cc): MaybeNormalizePath
    # upper-cases the drive letter, then hashes base::as_byte_span of the wide
    # path string - i.e. UTF-16LE bytes of the path as given (no lowercasing).
    if ($pathValue.Length -ge 2 -and
        $pathValue[0] -ge [char]'a' -and $pathValue[0] -le [char]'z' -and
        $pathValue[1] -eq ':') {
        $pathValue = [char]::ToUpperInvariant($pathValue[0]).ToString() + $pathValue.Substring(1)
    }
    $pathBytes = [Text.Encoding]::Unicode.GetBytes($pathValue)
    $sha256 = [Security.Cryptography.SHA256]::Create()
    try {
        $hash = $sha256.ComputeHash($pathBytes)
    }
    finally {
        $sha256.Dispose()
    }
    $idChars = [System.Collections.Generic.List[char]]::new()
    foreach ($byte in $hash[0..15]) {
        $idChars.Add([char](97 + ($byte -shr 4)))
        $idChars.Add([char](97 + ($byte -band 15)))
    }
    return (-join $idChars)
}

function Test-IsElevated {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    ([Security.Principal.WindowsPrincipal]$identity).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-ExtensionPolicyStatus {
    param(
        [Parameter(Mandatory)] [string]$Browser,
        [Parameter(Mandatory)] [string]$ExtensionId,
        [string]$PoliciesRootMachine = 'HKLM:\SOFTWARE\Policies',
        [string]$PoliciesRootUser = 'HKCU:\SOFTWARE\Policies'
    )

    $vendorKey = Get-ExtensionPolicyVendorKey $Browser
    if (-not $vendorKey) {
        return [pscustomobject]@{
            Applicable = $false
            Blocked = $false
            Allowlisted = $false
            VendorKey = $null
            MachineAllowlistSet = $false
            MachineBlocklistSet = $false
            ExtensionSettingsBlocked = $false
            WildcardBlocklist = $false
            IdBlocklisted = $false
        }
    }

    $machinePoliciesKey = Join-Path $PoliciesRootMachine $vendorKey
    $machineBlocklistKey = Join-Path $machinePoliciesKey 'ExtensionInstallBlocklist'
    $machineAllowlistKey = Join-Path $machinePoliciesKey 'ExtensionInstallAllowlist'
    $userPoliciesKey = Join-Path $PoliciesRootUser $vendorKey
    $userBlocklistKey = Join-Path $userPoliciesKey 'ExtensionInstallBlocklist'
    $userAllowlistKey = Join-Path $userPoliciesKey 'ExtensionInstallAllowlist'

    $machineBlocklist = Get-RegistryValueList $machineBlocklistKey
    $userBlocklist = Get-RegistryValueList $userBlocklistKey
    $machineAllowlist = Get-RegistryValueList $machineAllowlistKey
    $userAllowlist = Get-RegistryValueList $userAllowlistKey
    $machineAllowlistSet = Test-Path -LiteralPath $machineAllowlistKey

    $blocklist = if ($machineBlocklist.Count) { $machineBlocklist } else { $userBlocklist }
    $effectiveAllowlist = if ($machineAllowlistSet) { $machineAllowlist } else { $userAllowlist }
    $allowlisted = $effectiveAllowlist -contains $ExtensionId

    $settingsBlocked = $false
    if (Test-Path -LiteralPath $machinePoliciesKey) {
        $settingsRaw = (Get-ItemProperty -LiteralPath $machinePoliciesKey -ErrorAction SilentlyContinue).ExtensionSettings
        if (-not [string]::IsNullOrWhiteSpace($settingsRaw)) {
            try {
                $settings = $settingsRaw | ConvertFrom-Json
                $wildcard = $settings.'*'
                if ($null -ne $wildcard -and $wildcard.installation_mode -match 'block') {
                    $idEntry = $settings.$ExtensionId
                    if ($null -eq $idEntry -or $idEntry.installation_mode -ne 'allowed') {
                        $settingsBlocked = $true
                    }
                }
            }
            catch {
                $settingsBlocked = $false
            }
        }
    }

    # ExtensionSettings installation_mode=block overrides the allowlist. Per
    # Chromium UnpackedInstaller::IsLoadingUnpackedAllowed(), a wildcard
    # ExtensionInstallBlocklist entry blocks every unpacked extension - even
    # one explicitly listed in the allowlist.
    $wildcardBlocklist = [bool]($blocklist -contains '*')
    $idBlocklisted = [bool]($blocklist -contains $ExtensionId)
    $blocked = $settingsBlocked -or $wildcardBlocklist -or $idBlocklisted

    [pscustomobject]@{
        Applicable = $true
        Blocked = $blocked
        Allowlisted = $allowlisted
        VendorKey = $vendorKey
        MachineAllowlistSet = $machineAllowlistSet
        MachineBlocklistSet = $machineBlocklist.Count -gt 0
        ExtensionSettingsBlocked = $settingsBlocked
        WildcardBlocklist = $wildcardBlocklist
        IdBlocklisted = $idBlocklisted
    }
}

function Add-ExtensionAllowlistEntry {
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string]$Value
    )

    $nextIndex = 1
    if (Test-Path -LiteralPath $Path) {
        $existing = Get-ItemProperty -LiteralPath $Path -ErrorAction SilentlyContinue
        if ($existing) {
            foreach ($property in ($existing.PSObject.Properties | Where-Object { $_.Name -notmatch '^PS' })) {
                if ([string]$property.Value -eq $Value) {
                    return $false
                }
                if ($property.Name -match '^\d+$') {
                    $nextIndex = [Math]::Max($nextIndex, [int]$property.Name + 1)
                }
            }
        }
    }
    else {
        New-Item -Path $Path -Force | Out-Null
    }
    New-ItemProperty -LiteralPath $Path -Name "$nextIndex" -Value $Value -PropertyType String -Force | Out-Null
    return $true
}

function Add-ExtensionSettingsAllowed {
    param(
        [Parameter(Mandatory)] [string]$Browser,
        [Parameter(Mandatory)] [string]$ExtensionId,
        [string]$PoliciesRootMachine = 'HKLM:\SOFTWARE\Policies'
    )

    $vendorKey = Get-ExtensionPolicyVendorKey $Browser
    $policiesKey = Join-Path $PoliciesRootMachine $vendorKey
    if (-not (Test-Path -LiteralPath $policiesKey)) {
        New-Item -Path $policiesKey -Force | Out-Null
    }
    $settings = $null
    if (Test-Path -LiteralPath $policiesKey) {
        $settingsRaw = (Get-ItemProperty -LiteralPath $policiesKey -ErrorAction SilentlyContinue).ExtensionSettings
        if (-not [string]::IsNullOrWhiteSpace($settingsRaw)) {
            try {
                $settings = $settingsRaw | ConvertFrom-Json
            }
            catch {
                return $false
            }
        }
    }
    $idEntry = if ($settings) { $settings.$ExtensionId } else { $null }
    if ($idEntry -and $idEntry.installation_mode -eq 'allowed') {
        return $false
    }
    $object = if ($settings) { $settings } else { [pscustomobject]@{} }
    $object | Add-Member -NotePropertyName $ExtensionId -NotePropertyValue ([pscustomobject]@{ installation_mode = 'allowed' }) -Force
    Set-ItemProperty -LiteralPath $policiesKey -Name 'ExtensionSettings' -Value ($object | ConvertTo-Json -Depth 8 -Compress)
    return $true
}

function Remove-BlocklistEntry {
    param(
        [Parameter(Mandatory)] [string]$Path,
        [Parameter(Mandatory)] [string[]]$Values
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    $property = Get-ItemProperty -LiteralPath $Path -ErrorAction SilentlyContinue
    if ($null -eq $property) {
        return $false
    }
    # '*' removes every extension (including unpacked - Chromium
    # UnpackedInstaller::IsLoadingUnpackedAllowed); a specific ID entry
    # removes just this extension. Both must go for the extension to load.
    $targetNames = @($property.PSObject.Properties |
        Where-Object { $_.Name -notmatch '^PS' -and ([string]$_.Value -eq '*' -or $Values -contains [string]$_.Value) } |
        ForEach-Object { $_.Name })
    foreach ($name in $targetNames) {
        Remove-ItemProperty -LiteralPath $Path -Name $name -ErrorAction Stop
    }
    return ($targetNames.Count -gt 0)
}

function Set-MachineDebuggingPolicy {
    param(
        [Parameter(Mandatory)] [string]$Browser,
        [string]$PoliciesRootMachine = 'HKLM:\SOFTWARE\Policies'
    )

    $vendorKey = Get-ExtensionPolicyVendorKey $Browser
    if (-not $vendorKey) {
        return $false
    }
    $policiesKey = Join-Path $PoliciesRootMachine $vendorKey
    if (-not (Test-Path -LiteralPath $policiesKey)) {
        return $false
    }
    $property = Get-ItemProperty -LiteralPath $policiesKey -ErrorAction SilentlyContinue
    if ($null -eq $property) {
        return $false
    }
    $changed = $false
    # RemoteDebuggingAllowed=0 blocks CDP (Chrome 110+); 0 must become 1 for
    # the Extensions.loadUnpacked fallback to work.
    $remote = $property.RemoteDebuggingAllowed
    if ($null -ne $remote -and [int]$remote -eq 0) {
        Set-ItemProperty -LiteralPath $policiesKey -Name 'RemoteDebuggingAllowed' -Value 1 -Type DWord
        $changed = $true
    }
    # DeveloperToolsAvailability=2 disallows DevTools/CDP against extensions.
    $devtools = $property.DeveloperToolsAvailability
    if ($null -ne $devtools -and [int]$devtools -eq 2) {
        Set-ItemProperty -LiteralPath $policiesKey -Name 'DeveloperToolsAvailability' -Value 1 -Type DWord
        $changed = $true
    }
    return $changed
}

function Ensure-ExtensionPolicy {
    param(
        [Parameter(Mandatory)] [string]$Browser,
        [Parameter(Mandatory)] [string]$ExtensionId,
        [string]$PoliciesRootMachine = 'HKLM:\SOFTWARE\Policies',
        [string]$PoliciesRootUser = 'HKCU:\SOFTWARE\Policies',
        [Nullable[bool]]$IsElevated = $null
    )

    $status = Get-ExtensionPolicyStatus -Browser $Browser -ExtensionId $ExtensionId -PoliciesRootMachine $PoliciesRootMachine -PoliciesRootUser $PoliciesRootUser
    if (-not $status.Applicable -or -not $status.Blocked) {
        return [pscustomobject]@{
            Result = if ($status.Applicable) { 'not-blocked' } else { 'not-applicable' }
            Status = $status
        }
    }

    $elevated = if ($null -ne $IsElevated) { [bool]$IsElevated } else { Test-IsElevated }
    # Only user-scope policy values can be rewritten without elevation. A
    # machine-scope wildcard blocklist entry blocks every unpacked extension
    # (Chromium UnpackedInstaller::IsLoadingUnpackedAllowed), so an elevated
    # run is required to clear it before the browser starts.
    if (-not $elevated -and ($status.MachineAllowlistSet -or $status.MachineBlocklistSet -or $status.ExtensionSettingsBlocked)) {
        return [pscustomobject]@{ Result = 'needs-elevation'; Status = $status }
    }
    $writeRoot = if ($elevated) { $PoliciesRootMachine } else { $PoliciesRootUser }
    $blocklistKey = Join-Path $writeRoot ($status.VendorKey + '\ExtensionInstallBlocklist')
    $removedBlocklist = Remove-BlocklistEntry -Path $blocklistKey -Values @('*', $ExtensionId)
    $allowlistKey = Join-Path $writeRoot ($status.VendorKey + '\ExtensionInstallAllowlist')
    $added = Add-ExtensionAllowlistEntry -Path $allowlistKey -Value $ExtensionId
    if ($elevated) {
        Set-MachineDebuggingPolicy -Browser $Browser -PoliciesRootMachine $PoliciesRootMachine
        if ($status.ExtensionSettingsBlocked) {
            Add-ExtensionSettingsAllowed -Browser $Browser -ExtensionId $ExtensionId -PoliciesRootMachine $PoliciesRootMachine
        }
    }
    $refreshed = Get-ExtensionPolicyStatus -Browser $Browser -ExtensionId $ExtensionId -PoliciesRootMachine $PoliciesRootMachine -PoliciesRootUser $PoliciesRootUser
    $result = if ($elevated) { 'added-machine' } else { 'added-user' }
    if ($refreshed.Blocked) {
        $result = 'needs-manual-policy'
    }
    [pscustomobject]@{
        Result = $result
        Status = $refreshed
        AllowlistKey = $allowlistKey
        BlocklistKey = $blocklistKey
        Added = $added
        RemovedBlocklist = $removedBlocklist
    }
}

function New-FirefoxExtensionPackage {
    param(
        [Parameter(Mandatory)] [string]$SourceDir,
        [Parameter(Mandatory)] [string]$DestinationPath
    )

    if (-not (Test-Path (Join-Path $SourceDir 'manifest.json'))) {
        return $null
    }
    $parentDirectory = Split-Path -Parent $DestinationPath
    if (-not (Test-Path -LiteralPath $parentDirectory)) {
        New-Item -ItemType Directory -Path $parentDirectory -Force | Out-Null
    }
    if (Test-Path -LiteralPath $DestinationPath) {
        Remove-Item -LiteralPath $DestinationPath -Force -ErrorAction SilentlyContinue
    }
    $helperScript = Join-Path $PSScriptRoot 'make_xpi.py'
    if (-not (Test-Path $helperScript)) {
        return $null
    }
    & $pythonPath $helperScript $SourceDir $DestinationPath | Out-Null
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $DestinationPath)) {
        return $null
    }
    return $DestinationPath
}

function Ensure-FirefoxExtensionPolicy {
    param(
        [Parameter(Mandatory)] [string]$ExtensionId,
        [Parameter(Mandatory)] [string]$PackagePath,
        [bool]$IsElevated = (Test-IsElevated),
        [string]$PoliciesRoot = 'HKLM:\SOFTWARE\Policies\Mozilla\Firefox'
    )

    if (-not (Test-Path -LiteralPath $PackagePath)) {
        return [pscustomobject]@{ Result = 'package-unavailable' }
    }
    if (-not $IsElevated) {
        return [pscustomobject]@{ Result = 'needs-elevation' }
    }
    if (-not (Test-Path $PoliciesRoot)) {
        New-Item -Path $PoliciesRoot -Force | Out-Null
    }
    $installUri = [Uri]$PackagePath
    $entry = [pscustomobject]@{
        installation_mode = 'force_installed'
        install_url = $installUri.AbsoluteUri
    }
    $settingsObject = $null
    $settingsValue = (Get-ItemProperty -LiteralPath $PoliciesRoot -Name ExtensionSettings -ErrorAction SilentlyContinue).ExtensionSettings
    if ($settingsValue) {
        try {
            $settingsObject = $settingsValue | ConvertFrom-Json
        }
        catch {
            $settingsObject = $null
        }
    }
    if ($null -eq $settingsObject -or $settingsObject -isnot [pscustomobject]) {
        $settingsObject = [pscustomobject]@{}
    }
    if ($settingsObject.PSObject.Properties.Name -contains $ExtensionId) {
        $settingsObject.PSObject.Properties.Remove($ExtensionId)
    }
    $settingsObject | Add-Member -NotePropertyName $ExtensionId -NotePropertyValue $entry
    New-ItemProperty -LiteralPath $PoliciesRoot -Name ExtensionSettings `
        -Value ($settingsObject | ConvertTo-Json -Depth 8 -Compress) `
        -PropertyType String -Force | Out-Null

    $preferencesObject = $null
    $preferencesValue = (Get-ItemProperty -LiteralPath $PoliciesRoot -Name Preferences -ErrorAction SilentlyContinue).Preferences
    if ($preferencesValue) {
        try {
            $preferencesObject = $preferencesValue | ConvertFrom-Json
        }
        catch {
            $preferencesObject = $null
        }
    }
    if ($null -eq $preferencesObject -or $preferencesObject -isnot [pscustomobject]) {
        $preferencesObject = [pscustomobject]@{}
    }
    if ($preferencesObject.PSObject.Properties.Name -contains 'xpinstall.signatures.required') {
        $preferencesObject.PSObject.Properties.Remove('xpinstall.signatures.required')
    }
    $preferencesObject | Add-Member -NotePropertyName 'xpinstall.signatures.required' -NotePropertyValue $false
    New-ItemProperty -LiteralPath $PoliciesRoot -Name Preferences `
        -Value ($preferencesObject | ConvertTo-Json -Depth 8 -Compress) `
        -PropertyType String -Force | Out-Null
    return [pscustomobject]@{ Result = 'policy-installed' }
}

function Set-DeveloperModePreference {
    param([Parameter(Mandatory)] [string]$ProfileRoot)

    $defaultDirectory = Join-Path $ProfileRoot 'Default'
    $preferencesPath = Join-Path $defaultDirectory 'Preferences'
    if (-not (Test-Path -LiteralPath $defaultDirectory)) {
        New-Item -ItemType Directory -Path $defaultDirectory -Force | Out-Null
    }
    $settings = [pscustomobject]@{}
    if (Test-Path -LiteralPath $preferencesPath) {
        try {
            $settings = Get-Content -Raw -LiteralPath $preferencesPath | ConvertFrom-Json
        }
        catch {
            return $false
        }
    }
    $extensionsNode = $settings.extensions
    if ($null -eq $extensionsNode -or $extensionsNode -isnot [pscustomobject]) {
        $extensionsNode = [pscustomobject]@{}
    }
    $uiNode = $extensionsNode.ui
    if ($null -eq $uiNode -or $uiNode -isnot [pscustomobject]) {
        $uiNode = [pscustomobject]@{}
    }
    $uiNode | Add-Member -NotePropertyName 'developer_mode' -NotePropertyValue $true -Force
    $extensionsNode | Add-Member -NotePropertyName 'ui' -NotePropertyValue $uiNode -Force
    $settings | Add-Member -NotePropertyName 'extensions' -NotePropertyValue $extensionsNode -Force
    $settings | ConvertTo-Json -Depth 32 -Compress |
        Set-Content -LiteralPath $preferencesPath -Encoding UTF8
    return $true
}

function Install-ExtensionViaCDP {
    param(
        [Parameter(Mandatory)] [string]$HelperScript,
        [Parameter(Mandatory)] [string]$ExtensionPath,
        [string]$PythonExe = $pythonPath,
        [int]$Port = $debugPort,
        [int]$TimeoutSeconds = 30
    )

    if (-not (Test-Path -LiteralPath $HelperScript)) {
        return [pscustomobject]@{ Installed = $false; Error = 'helper-missing' }
    }
    $previousPreference = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $PythonExe $HelperScript --port $Port --path $ExtensionPath --timeout $TimeoutSeconds 2>&1
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
    $result = $null
    try {
        $result = ($output | Out-String).Trim() | ConvertFrom-Json
    }
    catch {
        $result = $null
    }
    if ($null -ne $result -and $result.ok) {
        return [pscustomobject]@{ Installed = $true; ExtensionId = $result.extension_id }
    }
    $errorText = if ($null -ne $result -and $result.error) { [string]$result.error } else { ($output | Out-String).Trim() }
    return [pscustomobject]@{ Installed = $false; Error = $errorText }
}

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
        'firefox' {
            @(
                (Join-Path $env:ProgramFiles 'Mozilla Firefox\firefox.exe'),
                (Join-Path ${env:ProgramFiles(x86)} 'Mozilla Firefox\firefox.exe'),
                (Join-Path $env:LOCALAPPDATA 'Mozilla Firefox\firefox.exe')
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
$extensionId = if ($Browser -eq 'comet') {
    $null
}
elseif ($Browser -eq 'firefox') {
    $firefoxExtensionId
}
else {
    Get-UnpackedExtensionId -Path $extensionPath
}
if ($Validate) {
    Write-Host "Browser: $Browser"
    Write-Host "Executable: $(if ($resolvedBrowserPath) { $resolvedBrowserPath } else { 'not required (bridge only)' })"
    if ($Browser -ne 'comet') {
        Write-Host "Extension: $extensionPath"
        Write-Host "Extension ID: $extensionId"
        if ($Browser -eq 'firefox') {
            Write-Host 'Extension install: Firefox packages this dist as an XPI and installs it through the Firefox enterprise policy when the terminal is elevated.'
        }
        else {
            $policyStatus = Get-ExtensionPolicyStatus -Browser $Browser -ExtensionId $extensionId
            if ($policyStatus.Blocked) {
                Write-Host "Extension policy: BLOCKED by enterprise policy (wildcard or ID blocklist). Re-run from an elevated terminal to clear it, ask IT to update policy, or use: ollama launch chromium" -ForegroundColor Yellow
            }
            else {
                Write-Host 'Extension policy: no blocking policy detected.'
            }
        }
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

$policyOutcome = $null
$firefoxXpiPath = $null
if ($Browser -eq 'firefox') {
    $firefoxXpiPath = Join-Path $appRoot 'autonomous-browser-assistant.xpi'
    $packagedPath = New-FirefoxExtensionPackage -SourceDir $extensionPath -DestinationPath $firefoxXpiPath
    if (-not $packagedPath) {
        Write-Host "The Firefox extension package could not be built from $extensionPath." -ForegroundColor Yellow
    }
    $policyOutcome = Ensure-FirefoxExtensionPolicy -ExtensionId $firefoxExtensionId -PackagePath $firefoxXpiPath
    switch ($policyOutcome.Result) {
        'policy-installed' {
            Write-Host 'Firefox enterprise policy updated: the Autonomous Browser Assistant XPI will install automatically on launch.' -ForegroundColor Green
        }
        'needs-elevation' {
            Write-Host 'Firefox can install the extension automatically, but writing the enterprise policy requires an elevated terminal.' -ForegroundColor Yellow
            Write-Host 'Re-run this launch from an Administrator terminal, or load it manually via about:debugging (Load Temporary Add-on).'
        }
        'package-unavailable' {
            Write-Host 'Skipping Firefox policy setup because the XPI package is unavailable.' -ForegroundColor Yellow
        }
        default { }
    }
}
elseif ($Browser -ne 'comet') {
    $policyOutcome = Ensure-ExtensionPolicy -Browser $Browser -ExtensionId $extensionId
    switch ($policyOutcome.Result) {
        'needs-elevation' {
            Write-Host 'The Autonomous Browser Assistant extension is blocked by enterprise extension policy.' -ForegroundColor Yellow
            Write-Host "Extension ID: $extensionId"
            Write-Host 'A wildcard (*) in the machine ExtensionInstallBlocklist blocks every extension, including unpacked ones; an allowlist entry cannot override it. Choose one:'
            Write-Host '  - Re-run this launch from an elevated (Administrator) terminal to remove the wildcard blocklist entry and allowlist the extension.'
            Write-Host '  - Ask IT to remove the wildcard (*) from the extension install blocklist, or add the extension ID above to the install allowlist.'
            Write-Host '  - Use "ollama launch chromium", which loads the extension automatically without policy changes.'
        }
        'needs-manual-policy' {
            Write-Host 'The extension was allowlisted, but an ExtensionSettings policy still blocks it.' -ForegroundColor Yellow
            Write-Host "Extension ID: $extensionId"
            Write-Host 'Ask IT to set "installation_mode": "allowed" for the extension ID above, or use "ollama launch chromium".'
        }
        'added-machine' {
            Write-Host 'Extension allowlisted in machine policy.' -ForegroundColor Green
        }
        'added-user' {
            Write-Host 'Extension allowlisted in user policy for this machine.' -ForegroundColor Green
        }
        default { }
    }
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
# Dedicated debug port for the Extensions.loadUnpacked CDP install (Chrome 137+
# dropped --load-extension in branded builds). Comet uses its own bridge debug
# port; chromium still accepts --load-extension and needs no CDP.
$extensionCdpPort = switch ($Browser) {
    'chrome' { 9224 }
    'edge' { 9225 }
    'firefox' { 9226 }
    default { $debugPort }
}
if ($Browser -eq 'comet') {
    $browserArguments = @(
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
elseif ($Browser -eq 'firefox') {
    @{
        bridgeUrl = "http://127.0.0.1:$port"
        token = $token
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $extensionPath 'runtime-config.json') -Encoding UTF8
    if (-not (Test-Path $profilePath)) {
        New-Item -ItemType Directory -Path $profilePath -Force | Out-Null
    }
    # Windows PowerShell 5.1 cannot bind a multi-statement subexpression (nested
    # array) inside an array literal to Start-Process -ArgumentList string[].
    # Build the argument list explicitly instead.
    $argumentList = [System.Collections.Generic.List[string]]::new()
    $argumentList.Add('-no-remote')
    $argumentList.Add("--profile=`"$profilePath`"")
    $argumentList.Add('--new-window')
    $argumentList.Add($assistantUrl)
    if (-not ($policyOutcome -and $policyOutcome.Result -eq 'policy-installed')) {
        # The policy only takes effect at Firefox startup, so when automatic
        # installation is not possible, open the temporary add-on page as well.
        $argumentList.Add('about:debugging#/runtime/this-firefox')
    }
    $browserArguments = $argumentList.ToArray()
}
else {
    @{
        bridgeUrl = "http://127.0.0.1:$port"
        token = $token
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $extensionPath 'runtime-config.json') -Encoding UTF8
    if ($Browser -ne 'chromium') {
        # Seed Developer mode so Load unpacked is available if manual fallback is needed.
        Set-DeveloperModePreference -ProfileRoot $profilePath | Out-Null
    }
    # Windows PowerShell 5.1 cannot bind a multi-statement subexpression (nested
    # array) inside an array literal to Start-Process -ArgumentList string[].
    # Build the argument list explicitly instead.
    $argumentList = [System.Collections.Generic.List[string]]::new()
    $argumentList.Add("`"--user-data-dir=$profilePath`"")
    if ($Browser -eq 'chromium') {
        $argumentList.Add("`"--load-extension=$extensionPath`"")
    }
    if ($Browser -ne 'chromium') {
        $argumentList.Add('--enable-unsafe-extension-debugging')
        $argumentList.Add("`"--remote-debugging-port=$extensionCdpPort`"")
    }
    $argumentList.Add('--no-first-run')
    $argumentList.Add('--new-window')
    $argumentList.Add($assistantUrl)
    $browserArguments = $argumentList.ToArray()
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
    $assistantReady = $false
    if ($Browser -eq 'chromium') {
        # Chromium accepts --load-extension, so the extension is already active.
        $assistantReady = $true
    }
    elseif ($Browser -eq 'firefox') {
        if ($policyOutcome -and $policyOutcome.Result -eq 'policy-installed') {
            $assistantReady = $true
        }
        elseif ($policyOutcome -and $policyOutcome.Result -eq 'package-unavailable') {
            Write-Host "The Firefox extension package could not be prepared: $firefoxXpiPath" -ForegroundColor Yellow
        }
    }
    else {
        $cdpResult = Install-ExtensionViaCDP -HelperScript (Join-Path $PSScriptRoot 'install_extension_cdp.py') -ExtensionPath $extensionPath -Port $extensionCdpPort
        if ($cdpResult.Installed) {
            $assistantReady = $true
            Write-Host "Autonomous Browser Assistant loaded automatically via the browser debug interface (extension ID: $($cdpResult.ExtensionId))." -ForegroundColor Green
        }
        else {
            Write-Host "Automatic extension installation failed: $($cdpResult.Error)" -ForegroundColor Yellow
        }
    }
    if ($Browser -ne 'firefox' -and $policyOutcome -and ($policyOutcome.Result -eq 'needs-elevation' -or $policyOutcome.Result -eq 'needs-manual-policy')) {
        Write-Host 'The extension is still blocked by enterprise policy - see the policy guidance above.' -ForegroundColor Yellow
    }
    elseif (-not $assistantReady) {
        if ($Browser -eq 'firefox') {
            Write-Host 'Opening about:debugging for one-time manual setup.' -ForegroundColor Yellow
            Write-Host 'Select Load Temporary Add-on and choose the package (or its unpacked folder):'
            Write-Host "  $firefoxXpiPath" -ForegroundColor Cyan
            Write-Host "  $extensionPath" -ForegroundColor Cyan
        }
        else {
            Write-Host 'Opening the extensions page for one-time manual setup.' -ForegroundColor Yellow
            Write-Host 'Enable Developer mode, select Load unpacked, and choose:'
            Write-Host "  $extensionPath" -ForegroundColor Cyan
            Start-Process -FilePath $resolvedBrowserPath -ArgumentList @(
                "`"--user-data-dir=$profilePath`"",
                $(if ($Browser -eq 'edge') { 'edge://extensions/' } else { 'chrome://extensions/' })
            ) | Out-Null
        }
    }
    Write-Host 'Pin Autonomous Browser Assistant and select its toolbar icon to open the side panel.'
}
