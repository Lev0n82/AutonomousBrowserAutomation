# -*- coding: utf-8 -*-
"""Tests for ollama-comet/launchpad.py: environment listing, preview gating,
sign-in classification (MFA / CAPTCHA / password expiry) and dispatch."""

import importlib.util
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "autonomous_browser_launchpad", ROOT / "ollama-comet" / "launchpad.py"
)
launchpad = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launchpad)


class FakeVaultError(Exception):
    pass


class FakeVaultClient:
    calls = []
    raise_error = None
    instances = []

    def __init__(self, config):
        self.config = config
        FakeVaultClient.instances.append(self)

    def login(self, executor, url, account, submit=False):
        FakeVaultClient.calls.append({"url": url, "account": account, "submit": submit})
        if FakeVaultClient.raise_error:
            raise FakeVaultError(FakeVaultClient.raise_error)


fake_vault_module = types.SimpleNamespace(
    VaultClient=FakeVaultClient, VaultError=FakeVaultError
)


class FakeExecutor:
    """Records tool calls and serves canned ReadPage payloads in order."""

    def __init__(self, pages=None):
        self.actions = []
        self.pages = list(pages or [])

    def __call__(self, payload):
        self.actions.append(payload)
        if payload.get("tool") == "ReadPage":
            if self.pages:
                return self.pages.pop(0)
            return {"text": ""}
        return {"ok": True}


CONFIG = {
    "environments": {
        "qa stack": {"url": "https://qa.example.com", "account": "qa@example.com"},
        "PR1 Production": {"url": "https://pr1.example.com", "account": "pr1@example.com"},
        "broken": {"url": "", "account": "x@example.com"},
    }
}


class LaunchpadToolsTests(unittest.TestCase):
    def setUp(self):
        FakeVaultClient.calls = []
        FakeVaultClient.raise_error = None
        FakeVaultClient.instances = []

    # ------------------------------------------------------------------
    # Listing / normalization

    def test_normalize_environments_filters_incomplete(self):
        envs = launchpad.normalize_environments(CONFIG)
        self.assertEqual(sorted(envs), ["PR1 Production", "qa stack"])

    def test_normalize_environments_accepts_json_string(self):
        envs = launchpad.normalize_environments(
            {"environments": '{"A": {"url": "https://a", "account": "a@x"}}'}
        )
        self.assertEqual(envs, {"A": {"url": "https://a", "account": "a@x"}})

    def test_list_environments_empty_has_note(self):
        result = launchpad.list_environments({})
        self.assertTrue(result["ok"])
        self.assertEqual(result["environments"], [])
        self.assertIn("No launchpad environments", result["note"])

    def test_list_environments_sorted(self):
        result = launchpad.list_environments(CONFIG)
        names = [item["name"] for item in result["environments"]]
        self.assertEqual(names, ["PR1 Production", "qa stack"])
        self.assertTrue(result["ok"])

    # ------------------------------------------------------------------
    # Launch gating

    def test_unknown_environment_is_error(self):
        with self.assertRaises(launchpad.LaunchpadError) as ctx:
            launchpad.launch_environment(
                CONFIG, None, fake_vault_module, "Nope", confirm=True, allow_sensitive=True
            )
        self.assertIn("Configured", str(ctx.exception))

    def test_preview_does_not_touch_executor(self):
        executor = FakeExecutor()
        result = launchpad.launch_environment(
            CONFIG, executor, fake_vault_module, "qa stack", confirm=False, allow_sensitive=True
        )
        self.assertEqual(result["status"], "preview")
        self.assertFalse(result["handoff"])
        self.assertEqual(executor.actions, [])
        self.assertEqual(FakeVaultClient.calls, [])

    def test_launch_without_sensitive_approval(self):
        executor = FakeExecutor()
        result = launchpad.launch_environment(
            CONFIG, executor, fake_vault_module, "qa stack", confirm=True, allow_sensitive=False
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "approval_required")
        self.assertEqual(FakeVaultClient.calls, [])

    def test_launch_without_executor_raises(self):
        with self.assertRaises(launchpad.LaunchpadError):
            launchpad.launch_environment(
                CONFIG, None, fake_vault_module, "qa stack", confirm=True, allow_sensitive=True
            )

    def test_launch_without_vault_module_raises(self):
        with self.assertRaises(launchpad.LaunchpadError):
            launchpad.launch_environment(
                CONFIG, FakeExecutor(), None, "qa stack", confirm=True, allow_sensitive=True
            )

    # ------------------------------------------------------------------
    # Successful / challenged launches

    def test_successful_launch_is_signed_in(self):
        executor = FakeExecutor(pages=[{"text": "Welcome back, dashboard home"}])
        result = launchpad.launch_environment(
            CONFIG, executor, fake_vault_module, "qa stack", confirm=True, allow_sensitive=True
        )
        self.assertEqual(result["status"], "signed_in")
        self.assertFalse(result["handoff"])
        self.assertEqual(result["account"], "qa@example.com")
        self.assertEqual(FakeVaultClient.calls[0]["submit"], True)
        self.assertIn("elapsed_seconds", result)

    def test_mfa_page_after_submit_is_handoff(self):
        executor = FakeExecutor(
            pages=[{"text": "Approve the request in your authenticator app"}]
        )
        result = launchpad.launch_environment(
            CONFIG, executor, fake_vault_module, "qa stack", confirm=True, allow_sensitive=True
        )
        self.assertEqual(result["status"], "mfa_required")
        self.assertTrue(result["handoff"])

    def test_captcha_page_after_error_is_handoff(self):
        FakeVaultClient.raise_error = "Sign in could not be completed."
        executor = FakeExecutor(pages=[{"text": "Please complete the captcha to continue"}])
        result = launchpad.launch_environment(
            CONFIG, executor, fake_vault_module, "qa stack", confirm=True, allow_sensitive=True
        )
        self.assertEqual(result["status"], "captcha_detected")
        self.assertTrue(result["handoff"])

    def test_expired_password_reports_reset_path(self):
        FakeVaultClient.raise_error = (
            "Login rejected; password appears expired or incorrect."
        )
        executor = FakeExecutor(pages=[{"text": "Your account has been signed out."}])
        result = launchpad.launch_environment(
            CONFIG, executor, fake_vault_module, "qa stack", confirm=True, allow_sensitive=True
        )
        self.assertEqual(result["status"], "password_expired")
        self.assertFalse(result["ok"])
        self.assertIn("VaultResetPassword", result["note"])

    def test_other_vault_error_propagates(self):
        FakeVaultClient.raise_error = "Network unreachable."
        executor = FakeExecutor(pages=[{"text": ""}])
        with self.assertRaises(launchpad.LaunchpadError) as ctx:
            launchpad.launch_environment(
                CONFIG, executor, fake_vault_module, "qa stack", confirm=True, allow_sensitive=True
            )
        self.assertIn("Network unreachable", str(ctx.exception))

    def test_login_form_still_present_after_submit(self):
        executor = FakeExecutor(
            pages=[{"text": "Sign in. Enter your password. Forgot password?"}]
        )
        result = launchpad.launch_environment(
            CONFIG, executor, fake_vault_module, "qa stack", confirm=True, allow_sensitive=True
        )
        self.assertEqual(result["status"], "login_form_still_present")
        self.assertTrue(result["handoff"])

    # ------------------------------------------------------------------
    # Dispatch

    def test_dispatch_lists_environments(self):
        result = launchpad.dispatch_tool("ListEnvironments", {}, CONFIG, True)
        self.assertTrue(result["ok"])

    def test_dispatch_requires_name(self):
        result = launchpad.dispatch_tool(
            "LaunchEnvironment", {}, CONFIG, True,
            executor=FakeExecutor(), vault_module=fake_vault_module,
        )
        self.assertIn("error", result)

    def test_dispatch_unknown_tool(self):
        result = launchpad.dispatch_tool("Nonsense", {}, CONFIG, True)
        self.assertIn("Unknown launchpad tool", result["error"])


if __name__ == "__main__":
    unittest.main()