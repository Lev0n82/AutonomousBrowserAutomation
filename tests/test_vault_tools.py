# -*- coding: utf-8 -*-
"""Tests for ollama-comet/vault.py: codec, masking, connect flows, secrets,
dispatch gating, browser login and the password-reset loop."""

import importlib.util
import json
import random
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "autonomous_browser_vault", ROOT / "ollama-comet" / "vault.py"
)
vault = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vault)


class FakeHttp:
    """Consumes an ordered list of (status, body) responses per URL substring."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def __call__(self, url, method="GET", body=None, headers=None, timeout=30):
        self.calls.append((url, method))
        if not self.routes:
            raise AssertionError(f"unexpected HTTP call: {method} {url}")
        status, text = self.routes.pop(0)
        if isinstance(text, (dict, list)):
            text = json.dumps(text)
        return status, text


class VaultToolsTests(unittest.TestCase):
    def setUp(self):
        self._original_request_http = vault.request_http
        self._original_post_form = vault._post_form
        self._original_sleep = vault.time.sleep
        self._original_cache_path = vault.token_cache_path
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_file = Path(self._tmp.name) / "vault-token.json"
        vault.token_cache_path = lambda: self.cache_file
        vault.VaultClient._PENDING_DEVICE["code"] = None

    def tearDown(self):
        vault.request_http = self._original_request_http
        vault._post_form = self._original_post_form
        vault.time.sleep = self._original_sleep
        vault.token_cache_path = self._original_cache_path
        vault.VaultClient._PENDING_DEVICE["code"] = None
        self._tmp.cleanup()

    # ------------------------------------------------------------------
    # Config helpers

    def companion_config(self):
        return {
            "vault_uri": "https://qa-dev-app.vault.azure.net",
            "vault_name": "qa-dev-app",
            "vault_api_port": 8080,
        }

    def direct_config(self):
        return {
            "vault_uri": "https://qa-dev-app.vault.azure.net",
            "vault_name": "qa-dev-app",
            "vault_api_port": 0,
        }

    def cache_token(self):
        self.cache_file.write_text(
            json.dumps({"access_token": "cached-token", "expires_on": 9999999999}),
            encoding="utf-8",
        )

    def client(self, config, routes):
        vault.request_http = FakeHttp(routes)
        return vault.VaultClient(config)

    # ------------------------------------------------------------------
    # Codec / helpers

    def test_secret_name_codec_round_trip(self):
        name = vault.email_to_secret_name("Jane_Doe@oag.on.ca")
        self.assertEqual(name, "jane---doe--oag-on-ca")
        self.assertEqual(
            vault.password_secret_name("Jane_Doe@oag.on.ca"),
            "jane---doe--oag-on-ca---password",
        )
        self.assertEqual(
            vault.email_from_secret_name("jane---doe--oag-on-ca---password"),
            "jane_doe@oag.on.ca",
        )

    def test_classify_email(self):
        for domain in ("uft@ontario.ca", "x@gov.on.ca", "a@oag.on.ca"):
            self.assertEqual(vault.classify_email(domain), "EntraID")
        self.assertEqual(vault.classify_email("qa.user@example.com"), "OPS-BPS-Secure")

    def test_mask_secret(self):
        self.assertEqual(vault.mask_secret("abcdefgh"), "abc***")
        self.assertEqual(vault.mask_secret("ab"), "***")
        self.assertEqual(vault.mask_secret(None), "***")

    def test_generate_password_format(self):
        password = vault.generate_password()
        self.assertEqual(len(password), 17)
        self.assertTrue(password.startswith("Q"))
        self.assertIn("_", password[:10])
        tail = password[9:]
        allowed = set(
            "abcdefghijklmnopqrstuvwxyz"
            "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
            "0123456789"
            + vault.SYMBOLS
        )
        self.assertTrue(set(tail).issubset(allowed))
        self.assertNotIn(" ", tail)
        self.assertNotIn('"', tail)
        self.assertNotIn("\\", tail)

    # ------------------------------------------------------------------
    # Connect

    def test_connect_companion_channel(self):
        client = self.client(
            self.companion_config(),
            [
                (200, {"apiKey": "pair-key"}),  # auth/pair
                (200, {}),  # /health
            ],
        )
        result = client.connect()
        self.assertEqual(result, {"ok": True, "mode": "companion", "vault": "qa-dev-app", "port": 8080})
        self.assertEqual(client._companion_api_key, "pair-key")

    def test_connect_companion_unhealthy(self):
        client = self.client(self.companion_config(), [(200, {"apiKey": "k"}), (500, "boom")])
        with self.assertRaises(vault.VaultError):
            client.connect()

    def test_connect_device_code_flow(self):
        client = self.client(self.direct_config(), [])
        vault._post_form = lambda *args, **kwargs: None

        def fake_post_form(url, fields):
            if "devicecode" in url:
                return (
                    200,
                    json.dumps(
                        {
                            "device_code": "DC123",
                            "user_code": "ABCD-1234",
                            "verification_uri": "https://microsoft.com/devicelogin",
                        }
                    ),
                )
            if "token" in url:
                return (200, json.dumps({"access_token": "fresh-token", "expires_in": 3599}))
            raise AssertionError(f"unexpected form post: {url}")

        vault._post_form = fake_post_form

        first = client.connect()
        self.assertEqual(vault.VaultClient._PENDING_DEVICE["code"], "DC123")
        self.assertTrue(first["pending"])
        self.assertEqual(first["user_code"], "ABCD-1234")

        second = client.connect()
        self.assertEqual(second["ok"], True)
        self.assertEqual(second["mode"], "direct")
        self.assertEqual(client._vault_token, "fresh-token")
        self.assertIsNone(vault.VaultClient._PENDING_DEVICE["code"])

    # ------------------------------------------------------------------
    # Secret operations

    def test_get_secret_not_found(self):
        self.cache_token()
        client = self.client(self.direct_config(), [(404, {"error": "nope"})])
        with self.assertRaises(vault.VaultError) as ctx:
            client.get_secret("missing")
        self.assertIn("Secret 'missing' not found.", str(ctx.exception))

    def test_get_secret_companion(self):
        client = self.client(
            self.companion_config(),
            [
                (200, {"apiKey": "pair-key"}),
                (200, {"value": "s3cret-value"}),
            ],
        )
        self.assertEqual(client.get_secret("some--email-com---password"), "s3cret-value")

    def test_list_secret_names_pagination(self):
        self.cache_token()
        client = self.client(
            self.direct_config(),
            [
                (
                    200,
                    {
                        "value": [{"name": "a--x-on-ca"}, {"name": "b--y-on-ca"}],
                        "nextLink": "https://qa-dev-app.vault.azure.net/secrets?api-version=7.4&skiptoken=2",
                    },
                ),
                (200, {"value": [{"name": "c--z-on-ca"}]}),
            ],
        )
        names = client.list_secret_names()
        self.assertEqual(names, ["a--x-on-ca", "b--y-on-ca", "c--z-on-ca"])

    def test_list_credentials_groups_by_email(self):
        client = self.client(self.companion_config(), [])
        client.list_secret_names = lambda: [
            "qauser--corp-on-ca",
            "qauser--corp-on-ca---password",
            "uft--ontario-ca",
            "uft--ontario-ca---password",
            "unrelated-name",
        ]
        accounts = client.list_credentials()
        self.assertEqual(
            accounts,
            [
                {
                    "email": "qauser@corp.on.ca",
                    "category": "OPS-BPS-Secure",
                    "has_password": True,
                },
                {
                    "email": "uft@ontario.ca",
                    "category": "EntraID",
                    "has_password": True,
                },
            ],
        )

    # ------------------------------------------------------------------
    # Dispatch gating

    def test_get_credential_masks_and_reveals(self):
        routes = [
            (200, {"apiKey": "pair-key"}),
            (200, {"value": "TopSecret99!"}),
            (200, {"apiKey": "pair-key"}),
            (200, {"value": "TopSecret99!"}),
        ]
        client = self.client(self.companion_config(), routes)
        client.list_secret_names = lambda: []

        masked = vault.dispatch_tool(
            "VaultGetCredential",
            {"email": "qa.user@corp.on.ca"},
            self.companion_config(),
            allow_sensitive=False,
            executor=None,
        )
        self.assertEqual(masked["password"], "Top***")
        self.assertFalse(masked["revealed"])

        revealed = vault.dispatch_tool(
            "VaultGetCredential",
            {"email": "qa.user@corp.on.ca", "reveal": True},
            self.companion_config(),
            allow_sensitive=True,
            executor=None,
        )
        self.assertEqual(revealed["password"], "TopSecret99!")
        self.assertTrue(revealed["revealed"])

    def test_dispatch_gates_login_and_reset(self):
        config = self.companion_config()
        blocked_login = vault.dispatch_tool(
            "VaultLogin", {"url": "https://x", "email": "a@b.c"}, config, False, None
        )
        self.assertIn("needs explicit approval", blocked_login["error"])

        blocked_reset = vault.dispatch_tool(
            "VaultResetPassword", {"email": "a@b.c"}, config, False, None
        )
        self.assertIn("confirm=true", blocked_reset["error"])

        no_executor = vault.dispatch_tool(
            "VaultLogin",
            {"url": "https://x", "email": "a@b.c", "submit": True},
            config,
            True,
            None,
        )
        self.assertIn("No browser session", no_executor["error"])

        unknown = vault.dispatch_tool("VaultNope", {}, config, True, None)
        self.assertIn("Unknown vault tool", unknown["error"])

    # ------------------------------------------------------------------
    # Browser login flow

    def _login_executor(self, page, second_page=None, final_page=None):
        calls = []

        def executor(command):
            calls.append(command)
            tool = command["tool"]
            if tool == "ReadPage":
                if command["arguments"]["filter"] == "text" and final_page is not None:
                    return final_page
                if len(calls) >= 4 and second_page is not None:
                    return second_page
                return page
            return {}

        return executor, calls

    def test_login_flow_two_step_and_submit(self):
        # dispatch_tool constructs its own VaultClient, so companion routes
        # (auth pair + password secret) must be served by the fake HTTP layer.
        routes = [
            (200, {"apiKey": "pair-key"}),
            (200, {"value": "VaultPass123!"}),
        ]
        self.client(self.companion_config(), routes)
        first_page = {
            "controls": [{"ref": "email-1", "label": "Email", "type": "email"}]
        }
        second_page = {
            "controls": [
                {"ref": "email-1", "label": "Email", "type": "email"},
                {"ref": "pass-1", "label": "Password", "type": "password"},
            ]
        }
        final_page = {"text": "Welcome back to OPS BPS Secure."}
        executor, calls = self._login_executor(first_page, second_page, final_page)

        result = vault.dispatch_tool(
            "VaultLogin",
            {"url": "https://stage.login.security.gov.on.ca", "email": "qa.user@corp.on.ca", "submit": True},
            self.companion_config(),
            True,
            executor,
        )
        self.assertEqual(result["ok"], True)
        self.assertEqual(result["category"], "OPS-BPS-Secure")
        self.assertEqual(result["submitted"], True)

        tools = [call["tool"] for call in calls]
        self.assertEqual(tools[0], "Navigate")
        self.assertIn("FormInput", tools)
        entered_password = [
            call for call in calls if call["tool"] == "FormInput"
        ][-1]["arguments"]["text"]
        self.assertEqual(entered_password, "VaultPass123!")
        self.assertIn("KEY", tools)

    def test_login_rejects_wrong_password(self):
        routes = [
            (200, {"apiKey": "pair-key"}),
            (200, {"value": "StalePass!"}),
        ]
        self.client(self.companion_config(), routes)
        first_page = {
            "controls": [{"ref": "pass-1", "label": "Password", "type": "password"}]
        }
        final_page = {"text": "Incorrect password. Please try again."}
        executor, _calls = self._login_executor(first_page, None, final_page)

        result = vault.dispatch_tool(
            "VaultLogin",
            {"url": "https://stage.login.security.gov.on.ca", "email": "qa.user@corp.on.ca", "submit": True},
            self.companion_config(),
            True,
            executor,
        )
        self.assertIn("expired or incorrect", result["error"])

    # ------------------------------------------------------------------
    # Password reset flow

    def test_reset_password_rotates_and_syncs(self):
        config = self.companion_config()
        client = self.client(
            config,
            [
                (200, {"apiKey": "pair-key"}),
                (200, {}),
            ],
        )
        client._check_account_guard = lambda: None
        client.fetch_otp = lambda started_iso, timeout_seconds=90: "54321"
        saved = {}
        client.set_secret = lambda name, value: saved.setdefault(name, value)

        vault.request_http = FakeHttp(
            [
                (200, {"result": 0}),  # forgotpassword
                (200, {"result": 0}),  # passwordreset
            ]
        )
        sleeps = []
        vault.time.sleep = lambda seconds: None

        result = client.reset_password("qa.user@corp.on.ca")
        self.assertEqual(result["ok"], True)
        new_password = saved["qa-user--corp-on-ca---password"]
        self.assertEqual(len(new_password), 17)
        self.assertEqual(result["password"], vault.mask_secret(new_password))
        self.assertNotEqual(result["password"], new_password)
        self.assertEqual(result["otp_hint"], vault.mask_secret("54321"))

    def test_reset_password_bps_failure_aborts(self):
        config = self.companion_config()
        client = self.client(config, [])
        client._check_account_guard = lambda: None
        vault.request_http = FakeHttp([(500, {"result": 5})])
        saved = []
        client.set_secret = lambda name, value: saved.append(name)

        with self.assertRaises(vault.VaultError) as ctx:
            client.reset_password("qa.user@corp.on.ca")
        self.assertIn("forgotpassword request failed", str(ctx.exception))
        self.assertEqual(saved, [])


if __name__ == "__main__":
    unittest.main()