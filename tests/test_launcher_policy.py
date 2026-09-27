# -*- coding: utf-8 -*-
"""Tests for the launcher's enterprise extension-policy handling.

The PowerShell policy functions inside ollama-comet/Launch-AutonomousBrowser.ps1
are extracted from the script and exercised through scenario harnesses run with
powershell.exe -NoProfile. The "machine" policy root is pointed at a disposable
HKCU registry root so machine-scope writes can be tested without elevation.

Semantics under test (verified against Chromium
UnpackedInstaller::IsLoadingUnpackedAllowed):
* a wildcard '*' ExtensionInstallBlocklist entry blocks every unpacked
  extension, even one explicitly allowlisted;
* an ID-specific blocklist entry blocks just that extension;
* machine-scope policies require an elevated run to rewrite;
* an elevated run removes blocklist entries and repairs RemoteDebuggingAllowed
  and DeveloperToolsAvailability so the CDP loadUnpacked fallback works.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_PATH = ROOT / 'ollama-comet' / 'Launch-AutonomousBrowser.ps1'
OLLAMA_CMD_PATH = ROOT / 'ollama-comet' / 'ollama.cmd'
MAKE_XPI_PATH = ROOT / 'ollama-comet' / 'make_xpi.py'

FUNCTION_NAMES = [
    'Get-ExtensionPolicyVendorKey',
    'Get-RegistryValueList',
    'Get-UnpackedExtensionId',
    'Test-IsElevated',
    'Get-ExtensionPolicyStatus',
    'Add-ExtensionAllowlistEntry',
    'Add-ExtensionSettingsAllowed',
    'Remove-BlocklistEntry',
    'Set-MachineDebuggingPolicy',
    'Ensure-ExtensionPolicy',
    'Ensure-FirefoxExtensionPolicy',
]

REG_ROOT = r'HKCU:\SOFTWARE\_OllamaCometPolicyTest'

ID_A = 'a' * 32
ID_B = 'b' * 32
ID_C = 'c' * 32

FIREFOX_EXTENSION_ID = 'autonomous-browser-assistant@ollama.local'

HARNESS_PRELUDE = """
function Set-TestBlocklist([string]$Key, [string[]]$Values) {
    New-Item -Path $Key -Force | Out-Null
    $i = 1
    foreach ($v in $Values) {
        New-ItemProperty -LiteralPath $Key -Name "$i" -Value $v -PropertyType String -Force | Out-Null
        $i++
    }
}
"""


def extract_function(source: str, name: str) -> str:
    marker = f'function {name} '
    index = source.find(marker)
    if index < 0:
        raise AssertionError(f'function {name} not found in launcher')
    open_brace = source.index('{', index)
    depth = 0
    for position in range(open_brace, len(source)):
        if source[position] == '{':
            depth += 1
        elif source[position] == '}':
            depth -= 1
            if depth == 0:
                return source[index:position + 1]
    raise AssertionError(f'function {name} braces unbalanced')


def expected_extension_id(path: str) -> str:
    # Chromium GenerateIdForPath: the path as given (drive letter upper-cased),
    # hashed as UTF-16LE bytes; each nibble of the first 16 digest bytes maps
    # to a-p.
    value = path
    if len(value) >= 2 and value[1] == ':':
        value = value[0].upper() + value[1:]
    digest = hashlib.sha256(value.encode('utf-16-le')).digest()[:16]
    return ''.join(chr(97 + (byte >> 4)) + chr(97 + (byte & 0xF)) for byte in digest)


class LauncherPolicyTests(unittest.TestCase):
    functions_text = ''

    @classmethod
    def setUpClass(cls):
        source = LAUNCHER_PATH.read_text(encoding='utf-8-sig')
        cls.functions_text = '\n'.join(
            extract_function(source, name) for name in FUNCTION_NAMES)
        subprocess.run(
            ['powershell.exe', '-NoProfile', '-Command',
             f'if (Test-Path -LiteralPath "{REG_ROOT}") '
             f'{{ Remove-Item -LiteralPath "{REG_ROOT}" -Recurse -Force }}'],
            capture_output=True, text=True)

    @classmethod
    def tearDownClass(cls):
        subprocess.run(
            ['powershell.exe', '-NoProfile', '-Command',
             f'if (Test-Path -LiteralPath "{REG_ROOT}") '
             f'{{ Remove-Item -LiteralPath "{REG_ROOT}" -Recurse -Force }}'],
            capture_output=True, text=True)

    def run_harness(self, scenario: str, body: str) -> dict:
        machine = f'{REG_ROOT}\\{scenario}\\machine'
        user = f'{REG_ROOT}\\{scenario}\\user'
        script = (
            "$ErrorActionPreference = 'Stop'\n"
            f"$machine = '{machine}'\n"
            f"$user = '{user}'\n"
            + HARNESS_PRELUDE + '\n'
            + self.functions_text + '\n'
            + body + '\n')
        completed = subprocess.run(
            ['powershell.exe', '-NoProfile', '-Command', script],
            capture_output=True, text=True, encoding='utf-8', errors='replace')
        if completed.returncode != 0:
            self.fail(
                'PowerShell harness failed:\n'
                + completed.stderr + '\n' + completed.stdout)
        return json.loads(completed.stdout)

    def test_unpacked_extension_id_matches_reference(self):
        result = self.run_harness(
            'idref',
            "Get-UnpackedExtensionId -Path 'C:\\Users\\Test\\Ext Browser' "
            '| ConvertTo-Json')
        self.assertEqual(result, expected_extension_id(r'C:\Users\Test\Ext Browser'))

    def test_unpacked_extension_id_tolerates_missing_path_and_resolves_existing(self):
        real_path = Path(os.environ.get('LOCALAPPDATA', '')) / (
            'AutonomousBrowserAutomation') / ('bin') / ('browser-extension')
        if not real_path.exists():
            self.skipTest('installed browser-extension directory not present')
        result = self.run_harness(
            'idreal',
            f"Get-UnpackedExtensionId -Path '{real_path}' | ConvertTo-Json")
        self.assertEqual(result, expected_extension_id(str(real_path)))

    def test_status_reports_not_blocked_without_policies(self):
        result = self.run_harness(
            'cleanstatus',
            "$status = Get-ExtensionPolicyStatus -Browser 'chrome' "
            '-ExtensionId ' + "'" + ID_A + "' "
            '-PoliciesRootMachine $machine -PoliciesRootUser $user\n'
            '[ordered]@{ applicable = $status.Applicable; blocked = $status.Blocked; '
            'vendorKey = $status.VendorKey } | ConvertTo-Json')
        self.assertTrue(result['applicable'])
        self.assertFalse(result['blocked'])
        self.assertEqual(result['vendorKey'], 'Google\\Chrome')

    def test_status_reports_wildcard_blocklist(self):
        result = self.run_harness(
            'wildstatus',
            "Set-TestBlocklist ($machine + '\\Google\\Chrome\\ExtensionInstallBlocklist') @('*')\n"
            '$status = Get-ExtensionPolicyStatus -Browser \'chrome\' -ExtensionId '
            + "'" + ID_A + "' "
            '-PoliciesRootMachine $machine -PoliciesRootUser $user\n'
            '[ordered]@{ blocked = $status.Blocked; wildcard = $status.WildcardBlocklist; '
            'idBlocklisted = $status.IdBlocklisted } | ConvertTo-Json')
        self.assertTrue(result['blocked'])
        self.assertTrue(result['wildcard'])
        self.assertFalse(result['idBlocklisted'])

    def test_status_reports_id_blocklist(self):
        result = self.run_harness(
            'idstatus',
            "Set-TestBlocklist ($machine + '\\Google\\Chrome\\ExtensionInstallBlocklist') @('" + ID_A + "')\n"
            '$status = Get-ExtensionPolicyStatus -Browser \'chrome\' -ExtensionId '
            + "'" + ID_A + "' "
            '-PoliciesRootMachine $machine -PoliciesRootUser $user\n'
            '[ordered]@{ blocked = $status.Blocked; wildcard = $status.WildcardBlocklist; '
            'idBlocklisted = $status.IdBlocklisted } | ConvertTo-Json')
        self.assertTrue(result['blocked'])
        self.assertFalse(result['wildcard'])
        self.assertTrue(result['idBlocklisted'])

    def test_registry_value_list_returns_empty_for_missing_key(self):
        result = self.run_harness(
            'emptylist',
            'ConvertTo-Json -InputObject @(Get-RegistryValueList -Path '
            "($machine + '\\Google\\Chrome\\Nope')) -Compress")
        self.assertEqual(result, [])

    def test_user_scope_wildcard_removed_without_elevation(self):
        result = self.run_harness(
            'userwrite',
            "Set-TestBlocklist ($user + '\\Google\\Chrome\\ExtensionInstallBlocklist') @('*')\n"
            '$out1 = Ensure-ExtensionPolicy -Browser chrome -ExtensionId ' + "'" + ID_A + "' "
            '-PoliciesRootMachine $machine -PoliciesRootUser $user -IsElevated $false\n'
            '$out2 = Ensure-ExtensionPolicy -Browser chrome -ExtensionId ' + "'" + ID_A + "' "
            '-PoliciesRootMachine $machine -PoliciesRootUser $user -IsElevated $false\n'
            '[ordered]@{ result = $out1.Result; removed = $out1.RemovedBlocklist; '
            "result2 = $out2.Result; blocked = $out2.Status.Blocked; values = "
            "@(Get-RegistryValueList ($user + '\\Google\\Chrome\\ExtensionInstallAllowlist')) } "
            '| ConvertTo-Json -Depth 3')
        self.assertEqual(result['result'], 'added-user')
        self.assertTrue(result['removed'])
        self.assertEqual(result['result2'], 'not-blocked')
        self.assertFalse(result['blocked'])
        self.assertEqual(result['values'], [ID_A])

    def test_needs_elevation_when_machine_policies_set(self):
        result = self.run_harness(
            'needselevation',
            "Set-TestBlocklist ($machine + '\\Google\\Chrome\\ExtensionInstallBlocklist') @('*')\n"
            "Set-TestBlocklist ($machine + '\\Google\\Chrome\\ExtensionInstallAllowlist') @('" + ID_B + "')\n"
            '$out = Ensure-ExtensionPolicy -Browser chrome -ExtensionId ' + "'" + ID_A + "' "
            '-PoliciesRootMachine $machine -PoliciesRootUser $user -IsElevated $false\n'
            "[ordered]@{ result = $out.Result; userKey = "
            "(Test-Path ($user + '\\Google\\Chrome\\ExtensionInstallAllowlist')) } "
            '| ConvertTo-Json')
        self.assertEqual(result['result'], 'needs-elevation')
        self.assertFalse(result['userKey'])

    def test_allowlist_cannot_rescue_wildcard_blocked_unpacked(self):
        result = self.run_harness(
            'allowlisted',
            "Set-TestBlocklist ($machine + '\\Google\\Chrome\\ExtensionInstallBlocklist') @('*')\n"
            "Set-TestBlocklist ($machine + '\\Google\\Chrome\\ExtensionInstallAllowlist') @('" + ID_A + "')\n"
            '$out = Ensure-ExtensionPolicy -Browser chrome -ExtensionId ' + "'" + ID_A + "' "
            '-PoliciesRootMachine $machine -PoliciesRootUser $user -IsElevated $false\n'
            '[ordered]@{ result = $out.Result; allowlisted = $out.Status.Allowlisted; '
            'wildcard = $out.Status.WildcardBlocklist; blocked = $out.Status.Blocked } '
            '| ConvertTo-Json -Depth 3')
        self.assertEqual(result['result'], 'needs-elevation')
        self.assertTrue(result['allowlisted'])
        self.assertTrue(result['wildcard'])
        self.assertTrue(result['blocked'])

    def test_machine_write_when_elevated_removes_wildcard(self):
        result = self.run_harness(
            'machinewrite',
            "Set-TestBlocklist ($machine + '\\Google\\Chrome\\ExtensionInstallBlocklist') @('*')\n"
            "Set-TestBlocklist ($machine + '\\Google\\Chrome\\ExtensionInstallAllowlist') @('" + ID_B + "','" + ID_C + "')\n"
            '$out = Ensure-ExtensionPolicy -Browser chrome -ExtensionId ' + "'" + ID_A + "' "
            '-PoliciesRootMachine $machine -PoliciesRootUser $user -IsElevated $true\n'
            '$property = Get-ItemProperty -LiteralPath '
            "($machine + '\\Google\\Chrome\\ExtensionInstallAllowlist')\n"
            "[ordered]@{ result = $out.Result; removed = $out.RemovedBlocklist; value3 = "
            "$property.'3'; blockAfter = "
            "@(Get-RegistryValueList ($machine + '\\Google\\Chrome\\ExtensionInstallBlocklist')) } "
            '| ConvertTo-Json -Depth 3')
        self.assertEqual(result['result'], 'added-machine')
        self.assertTrue(result['removed'])
        self.assertEqual(result['value3'], ID_A)
        self.assertEqual(result['blockAfter'], [])

    def test_elevated_run_repairs_debugging_policy(self):
        result = self.run_harness(
            'debugpolicy',
            "New-Item -Path ($machine + '\\Google\\Chrome') -Force | Out-Null\n"
            # New-Item -Force on an existing parent key deletes its subkeys, so the
            # blocklist must be created AFTER the parent vendor key exists.
            "Set-TestBlocklist ($machine + '\\Google\\Chrome\\ExtensionInstallBlocklist') @('*')\n"
            'Set-ItemProperty -LiteralPath ($machine + \'\\Google\\Chrome\') '
            '-Name RemoteDebuggingAllowed -Value 0 -Type DWord\n'
            'Set-ItemProperty -LiteralPath ($machine + \'\\Google\\Chrome\') '
            '-Name DeveloperToolsAvailability -Value 2 -Type DWord\n'
            '$out = Ensure-ExtensionPolicy -Browser chrome -ExtensionId ' + "'" + ID_A + "' "
            '-PoliciesRootMachine $machine -PoliciesRootUser $user -IsElevated $true\n'
            '$prop = Get-ItemProperty -LiteralPath ($machine + \'\\Google\\Chrome\')\n'
            '[ordered]@{ result = $out.Result; remote = $prop.RemoteDebuggingAllowed; '
            'devtools = $prop.DeveloperToolsAvailability } | ConvertTo-Json')
        self.assertEqual(result['result'], 'added-machine')
        self.assertEqual(result['remote'], 1)
        self.assertEqual(result['devtools'], 1)

    def test_set_machine_debugging_policy_is_minimal_and_idempotent(self):
        result = self.run_harness(
            'debugminimal',
            "New-Item -Path ($machine + '\\Google\\Chrome') -Force | Out-Null\n"
            'Set-ItemProperty -LiteralPath ($machine + \'\\Google\\Chrome\') '
            '-Name RemoteDebuggingAllowed -Value 0 -Type DWord\n'
            'Set-ItemProperty -LiteralPath ($machine + \'\\Google\\Chrome\') '
            '-Name DeveloperToolsAvailability -Value 2 -Type DWord\n'
            'Set-ItemProperty -LiteralPath ($machine + \'\\Google\\Chrome\') '
            '-Name Homepage -Value \'https://example.com\' -Type String\n'
            '$changed1 = Set-MachineDebuggingPolicy -Browser chrome -PoliciesRootMachine $machine\n'
            '$changed2 = Set-MachineDebuggingPolicy -Browser chrome -PoliciesRootMachine $machine\n'
            '$prop = Get-ItemProperty -LiteralPath ($machine + \'\\Google\\Chrome\')\n'
            '[ordered]@{ changed1 = $changed1; changed2 = $changed2; '
            'remote = $prop.RemoteDebuggingAllowed; devtools = $prop.DeveloperToolsAvailability; '
            'homepage = $prop.Homepage } | ConvertTo-Json')
        self.assertTrue(result['changed1'])
        self.assertFalse(result['changed2'])
        self.assertEqual(result['remote'], 1)
        self.assertEqual(result['devtools'], 1)
        self.assertEqual(result['homepage'], 'https://example.com')

    def test_set_machine_debugging_policy_skips_missing_vendor_key(self):
        result = self.run_harness(
            'debugmissing',
            'Set-MachineDebuggingPolicy -Browser chrome -PoliciesRootMachine $machine '
            '| ConvertTo-Json')
        self.assertFalse(result)

    def test_id_specific_blocklist_entry_removed_when_elevated(self):
        result = self.run_harness(
            'idremoval',
            "Set-TestBlocklist ($machine + '\\Google\\Chrome\\ExtensionInstallBlocklist') @('" + ID_B + "','" + ID_A + "')\n"
            '$out = Ensure-ExtensionPolicy -Browser chrome -ExtensionId ' + "'" + ID_A + "' "
            '-PoliciesRootMachine $machine -PoliciesRootUser $user -IsElevated $true\n'
            '[ordered]@{ result = $out.Result; removed = $out.RemovedBlocklist; '
            'blocked = $out.Status.Blocked; idBlocklisted = $out.Status.IdBlocklisted; '
            'blockAfter = '
            "@(Get-RegistryValueList ($machine + '\\Google\\Chrome\\ExtensionInstallBlocklist')) } "
            '| ConvertTo-Json -Depth 3')
        self.assertEqual(result['result'], 'added-machine')
        self.assertTrue(result['removed'])
        self.assertFalse(result['blocked'])
        self.assertFalse(result['idBlocklisted'])
        self.assertEqual(result['blockAfter'], [ID_B])

    def test_non_wildcard_blocklist_does_not_block(self):
        result = self.run_harness(
            'nonwildcard',
            "Set-TestBlocklist ($machine + '\\Google\\Chrome\\ExtensionInstallBlocklist') @('" + ID_B + "')\n"
            '$out = Ensure-ExtensionPolicy -Browser chrome -ExtensionId ' + "'" + ID_A + "' "
            '-PoliciesRootMachine $machine -PoliciesRootUser $user -IsElevated $false\n'
            '$out | ConvertTo-Json -Depth 3')
        self.assertEqual(result['Result'], 'not-blocked')

    def test_chromium_goes_through_policy_flow(self):
        result = self.run_harness(
            'chromium',
            '$out = Ensure-ExtensionPolicy -Browser chromium -ExtensionId ' + "'" + ID_A + "' "
            '-PoliciesRootMachine $machine -PoliciesRootUser $user\n'
            '$out | ConvertTo-Json -Depth 3')
        self.assertEqual(result['Result'], 'not-blocked')
        self.assertEqual(result['Status']['VendorKey'], 'Chromium')

    def test_extension_settings_blocked_fixed_when_elevated(self):
        result = self.run_harness(
            'settings-elevated',
            "New-Item -Path ($machine + '\\Google\\Chrome') -Force | Out-Null\n"
            "Set-ItemProperty -LiteralPath ($machine + '\\Google\\Chrome') "
            "-Name ExtensionSettings -Value '{\"*\":{\"installation_mode\":\"blocked\"}}'\n"
            '$out = Ensure-ExtensionPolicy -Browser chrome -ExtensionId ' + "'" + ID_A + "' "
            '-PoliciesRootMachine $machine -PoliciesRootUser $user -IsElevated $true\n'
            '$json = (Get-ItemProperty -LiteralPath '
            "($machine + '\\Google\\Chrome')).ExtensionSettings | ConvertFrom-Json\n"
            "[ordered]@{ result = $out.Result; mode = $json." + ID_A + ".installation_mode; "
            "blocked = $out.Status.Blocked } | ConvertTo-Json -Depth 3")
        self.assertEqual(result['result'], 'added-machine')
        self.assertEqual(result['mode'], 'allowed')
        self.assertFalse(result['blocked'])

    def test_extension_settings_blocked_needs_elevation(self):
        result = self.run_harness(
            'settings-user',
            "New-Item -Path ($machine + '\\Google\\Chrome') -Force | Out-Null\n"
            "Set-ItemProperty -LiteralPath ($machine + '\\Google\\Chrome') "
            "-Name ExtensionSettings -Value '{\"*\":{\"installation_mode\":\"blocked\"}}'\n"
            '$out = Ensure-ExtensionPolicy -Browser chrome -ExtensionId ' + "'" + ID_A + "' "
            '-PoliciesRootMachine $machine -PoliciesRootUser $user -IsElevated $false\n'
            "[ordered]@{ result = $out.Result; userKey = "
            "(Test-Path ($user + '\\Google\\Chrome\\ExtensionInstallAllowlist')) } "
            '| ConvertTo-Json')
        self.assertEqual(result['result'], 'needs-elevation')
        self.assertFalse(result['userKey'])

    def test_vendor_keys_map_correctly(self):
        result = self.run_harness(
            'vendormap',
            "[ordered]@{ chrome = Get-ExtensionPolicyVendorKey chrome; "
            'edge = Get-ExtensionPolicyVendorKey edge; '
            'chromium = Get-ExtensionPolicyVendorKey chromium; '
            'comet = Get-ExtensionPolicyVendorKey comet; '
            'firefox = Get-ExtensionPolicyVendorKey firefox } | ConvertTo-Json')
        self.assertEqual(result['chrome'], 'Google\\Chrome')
        self.assertEqual(result['edge'], 'Microsoft\\Edge')
        self.assertEqual(result['chromium'], 'Chromium')
        self.assertIsNone(result['comet'])
        self.assertIsNone(result['firefox'])

    def test_firefox_policy_needs_elevation_when_not_elevated(self):
        result = self.run_harness(
            'firefox-needs-elevation',
            "$pkg = [System.IO.Path]::GetTempFileName()\n"
            '$out = Ensure-FirefoxExtensionPolicy -ExtensionId '
            + "'" + FIREFOX_EXTENSION_ID + "' -PackagePath $pkg "
            '-IsElevated $false -PoliciesRoot $machine\n'
            'Remove-Item -LiteralPath $pkg -Force\n'
            "[ordered]@{ result = $out.Result; rootCreated = "
            '(Test-Path $machine) } | ConvertTo-Json')
        self.assertEqual(result['result'], 'needs-elevation')
        self.assertFalse(result['rootCreated'])

    def test_firefox_policy_unavailable_without_package(self):
        result = self.run_harness(
            'firefox-missing-package',
            '$out = Ensure-FirefoxExtensionPolicy -ExtensionId '
            + "'" + FIREFOX_EXTENSION_ID + "' "
            "-PackagePath (Join-Path $machine 'missing.xpi') "
            '-IsElevated $true -PoliciesRoot $machine\n'
            "[ordered]@{ result = $out.Result; rootCreated = "
            '(Test-Path $machine) } | ConvertTo-Json')
        self.assertEqual(result['result'], 'package-unavailable')
        self.assertFalse(result['rootCreated'])

    def test_firefox_policy_installed_when_elevated(self):
        result = self.run_harness(
            'firefox-install',
            "$pkg = [System.IO.Path]::GetTempFileName()\n"
            '$out = Ensure-FirefoxExtensionPolicy -ExtensionId '
            + "'" + FIREFOX_EXTENSION_ID + "' -PackagePath $pkg "
            '-IsElevated $true -PoliciesRoot $machine\n'
            '$settings = (Get-ItemProperty -LiteralPath $machine '
            '-Name ExtensionSettings).ExtensionSettings | ConvertFrom-Json\n'
            '$entry = $settings.' + "'" + FIREFOX_EXTENSION_ID + "'\n"
            '$prefs = (Get-ItemProperty -LiteralPath $machine '
            '-Name Preferences).Preferences | ConvertFrom-Json\n'
            'Remove-Item -LiteralPath $pkg -Force\n'
            "[ordered]@{ result = $out.Result; mode = $entry.installation_mode; "
            'url = $entry.install_url; '
            "signed = $prefs.'xpinstall.signatures.required' } | ConvertTo-Json")
        self.assertEqual(result['result'], 'policy-installed')
        self.assertEqual(result['mode'], 'force_installed')
        self.assertTrue(result['url'].startswith('file:///'))
        self.assertFalse(result['signed'])

    def test_firefox_policy_preserves_existing_entries(self):
        result = self.run_harness(
            'firefox-merge',
            "New-Item -Path $machine -Force | Out-Null\n"
            "Set-ItemProperty -LiteralPath $machine -Name ExtensionSettings "
            '-Value \'{"other@x.1":{"installation_mode":"blocked"}}\'\n'
            "Set-ItemProperty -LiteralPath $machine -Name Preferences "
            "-Value '{\"some.pref\":true}'\n"
            "$pkg = Join-Path $machine 'addon.xpi'\n"
            'New-Item -ItemType File -Path $pkg -Force | Out-Null\n'
            '$out = Ensure-FirefoxExtensionPolicy -ExtensionId '
            + "'" + FIREFOX_EXTENSION_ID + "' -PackagePath $pkg "
            '-IsElevated $true -PoliciesRoot $machine\n'
            '$settings = (Get-ItemProperty -LiteralPath $machine '
            '-Name ExtensionSettings).ExtensionSettings | ConvertFrom-Json\n'
            '$prefs = (Get-ItemProperty -LiteralPath $machine '
            '-Name Preferences).Preferences | ConvertFrom-Json\n'
            "[ordered]@{ result = $out.Result; "
            "otherMode = $settings.'other@x.1'.installation_mode; "
            "somePref = $prefs.'some.pref'; "
            "signed = $prefs.'xpinstall.signatures.required' } | ConvertTo-Json")
        self.assertEqual(result['result'], 'policy-installed')
        self.assertEqual(result['otherMode'], 'blocked')
        self.assertTrue(result['somePref'])
        self.assertFalse(result['signed'])


class FirefoxLauncherWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.launcher_text = LAUNCHER_PATH.read_text(encoding='utf-8-sig')

    def test_validate_set_includes_firefox(self):
        self.assertIn("'firefox'", self.launcher_text.splitlines()[3])

    def test_firefox_extension_id_declared_in_launcher(self):
        self.assertIn(FIREFOX_EXTENSION_ID, self.launcher_text)

    def test_ollama_cmd_routes_firefox(self):
        cmd_text = OLLAMA_CMD_PATH.read_text(encoding='ascii', errors='replace')
        self.assertIn('firefox', cmd_text.lower())
        self.assertIn(':firefox', cmd_text)
        self.assertIn('Browser firefox', cmd_text)

    def test_make_xpi_builds_zip_with_manifest_at_root(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / 'dist'
            (source / 'subdir').mkdir(parents=True)
            (source / 'manifest.json').write_text('{"name": "test"}')
            (source / 'subdir' / 'nested.js').write_text('// nested')
            destination = Path(temp) / 'out' / 'deep' / 'addon.xpi'
            completed = subprocess.run(
                [sys.executable, str(MAKE_XPI_PATH),
                 str(source), str(destination)],
                capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertTrue(destination.is_file())
            with zipfile.ZipFile(destination) as archive:
                names = archive.namelist()
            self.assertIn('manifest.json', names)
            self.assertIn('subdir/nested.js', names)
            self.assertFalse(any('\\' in name for name in names))


if __name__ == '__main__':
    unittest.main()