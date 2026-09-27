"""Azure Key Vault + EntraID/OPS-BPS credential tools for the browser agent.

Provides the VaultClient and dispatch layer behind the Vault* tools advertised
by bridge.py. Two access channels are supported:

1. companion  - the local companion app (http://127.0.0.1:{port}) proxies the
   vault using its own managed identity; no interactive sign-in is needed.
2. direct     - the agent obtains its own AAD token via the OAuth device-code
   flow, cached under %LOCALAPPDATA%\\OllamaComet\\vault-token.json.

Secret codec (mirrors the companion app naming convention):
    user@ontario.ca     -> user--ontario-ca
    user@ontario.ca     -> user--ontario-ca---password   (password secret)
Decoding reverses the mapping so VaultListCredentials can group credentials
by email address.

Password reset (OPS-BPS secure) flow:
    forgotpassword -> OTP mailed to the reset mailbox (read via Graph ROPC
    using the reset email account) -> passwordreset with OTP -> sync new
    password back into the vault.
"""

from __future__ import annotations

import json
import random
import re
import string
import time
import urllib.parse
import urllib.request
from pathlib import Path

VAULT_API_VERSION = "7.4"
BPS_BASE = "https://stage.login.security.gov.on.ca"
SYMBOLS = "!#$%&()*+,-./:;<=>?@[]^_{|}~"
AUTH_SCHEME = "Bearer"


class VaultError(RuntimeError):
    """Raised for vault, Graph, or flow failures with a user-facing message."""


# ---------------------------------------------------------------------------
# HTTP helpers


def request_http(url, method="GET", body=None, headers=None, timeout=30):
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8") if isinstance(body, (dict, list)) else body
    request = urllib.request.Request(url, data=data, method=method)
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.getcode(), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            payload = error.read().decode("utf-8", "replace")
        except Exception:
            payload = ""
        return error.code, payload


def _post_form(url, fields):
    data = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.getcode(), response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        try:
            payload = error.read().decode("utf-8", "replace")
        except Exception:
            payload = ""
        return error.code, payload


def token_cache_path():
    import os

    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    return Path(base) / "OllamaComet" / "vault-token.json"


# ---------------------------------------------------------------------------
# Codec helpers


def email_to_secret_name(email):
    name = email.strip().lower()
    name = name.replace("_", "---").replace("@", "--").replace(".", "-")
    return name


def password_secret_name(email):
    return email_to_secret_name(email) + "---password"


def email_from_secret_name(name):
    if not name:
        return None
    raw = name
    if raw.endswith("---password"):
        raw = raw[: -len("---password")]
    raw = raw.replace("---", "\x00")
    at = raw.find("--")
    if at < 0:
        return None
    local = raw[:at].replace("\x00", "_")
    domain = raw[at + 2 :].replace("\x00", "_").replace("-", ".")
    return f"{local}@{domain}"


def classify_email(email):
    domain = (email or "").split("@")[-1].lower()
    if domain in ("ontario.ca", "gov.on.ca", "oag.on.ca", "mgs.gov.on.ca"):
        return "EntraID"
    return "OPS-BPS-Secure"


def mask_secret(value, keep=3):
    text = str(value or "")
    if len(text) <= keep:
        return "***"
    return text[:keep] + "***"


def generate_password():
    """Return a 17-char password: 'Q' + MMDD_YYYY + 7 random characters."""
    core = "Q" + time.strftime("%m%d_%Y")
    pool = string.ascii_letters + string.digits + SYMBOLS
    tail = "".join(random.SystemRandom().choice(pool) for _ in range(7))
    return core + tail


# ---------------------------------------------------------------------------
# Vault client


class VaultClient:
    """Talks to Azure Key Vault via the companion app or direct device flow."""

    _PENDING_DEVICE = {"code": None}

    def __init__(self, config):
        self.vault_uri = (config.get("vault_uri") or "").rstrip("/")
        self.vault_name = config.get("vault_name") or ""
        self.api_port = 8080 if config.get("vault_api_port") in (None, "") else int(
            config.get("vault_api_port")
        )
        self.api_key = config.get("vault_api_key") or ""
        self.api_key_secret_uri = config.get("vault_api_key_secret_uri") or ""
        self.tenant_id = config.get("azure_tenant_id") or config.get("graph_tenant_id") or ""
        self.client_id = config.get("azure_client_id") or config.get("graph_client_id") or ""
        self.reset_email = config.get("reset_email") or ""
        self.graph_tenant = config.get("graph_tenant_id") or self.tenant_id
        self.keychain_uris = {
            "dev": config.get("keychain_dev_uri") or "",
            "qa": config.get("keychain_qa_uri") or "",
        }
        self._vault_token = None
        self._companion_api_key = None
        if not self.vault_uri and self.vault_name:
            self.vault_uri = f"https://{self.vault_name}.vault.azure.net"

    # ------------------------------------------------------------------
    # Channel selection

    def _channel(self):
        return "companion" if self.api_port else "direct"

    # ------------------------------------------------------------------
    # Companion channel

    def _companion_headers(self):
        if not self._companion_api_key:
            status, body = request_http(
                f"http://127.0.0.1:{self.api_port}/api/auth/pair", "GET", None, None, 10
            )
            if status == 200 and body:
                self._companion_api_key = str(json.loads(body).get("apiKey") or "")
        headers = {"Content-Type": "application/json"}
        if self.api_key or self._companion_api_key:
            headers["X-Api-Key"] = self.api_key or self._companion_api_key
        return headers

    def _companion_request(self, method, path, body):
        headers = self._companion_headers()
        url = f"http://127.0.0.1:{self.api_port}{path}"
        status, text = request_http(url, method, body, headers, 30)
        if status >= 400:
            raise VaultError(f"Companion API HTTP {status} for {path}: {text[:200]}")
        return json.loads(text) if text else {}

    # ------------------------------------------------------------------
    # Direct channel

    def _cache_file(self):
        return token_cache_path()

    def _load_cached_token(self):
        if self._vault_token is not None:
            return self._vault_token
        cache_file = self._cache_file()
        if cache_file.exists():
            try:
                payload = json.loads(cache_file.read_text(encoding="utf-8"))
                if payload.get("access_token") and payload.get(
                    "expires_on", 0
                ) > time.time() + 60:
                    self._vault_token = payload["access_token"]
                    return self._vault_token
            except (OSError, ValueError):
                pass
        return None

    def _save_token(self, token_payload):
        cache_file = self._cache_file()
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "access_token": token_payload["access_token"],
            "expires_on": time.time() + int(token_payload.get("expires_in", 3599)),
        }
        cache_file.write_text(json.dumps(record), encoding="utf-8")
        self._vault_token = token_payload["access_token"]

    def _device_code_start(self):
        status, body = _post_form(
            f"https://login.microsoftonline.com/{self.tenant_id}"
            "/oauth2/v2.0/devicecode",
            {
                "client_id": self.client_id,
                "scope": "https://vault.azure.net/.default offline_access",
            },
        )
        payload = json.loads(body) if body else {}
        if status != 200 or "device_code" not in payload:
            raise VaultError(
                f"Device-code start failed (HTTP {status}): {body[:200]}"
            )
        return payload

    def _device_code_poll(self, device_code):
        status, body = _post_form(
            f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token",
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": self.client_id,
                "device_code": device_code,
            },
        )
        payload = json.loads(body) if body else {}
        if status == 200 and "access_token" in payload:
            self._save_token(payload)
            return True
        error = str(payload.get("error", ""))
        if error in ("authorization_pending", "slow_down"):
            return False
        raise VaultError(
            f"Device-code polling failed: {error or body[:200]}"
        )

    # ------------------------------------------------------------------
    # Connection

    def connect(self):
        if self._channel() == "companion":
            try:
                self._companion_request("GET", "/health", None)
            except VaultError as error:
                raise VaultError(f"Companion app health check failed: {error}") from None
            return {
                "ok": True,
                "mode": "companion",
                "vault": self.vault_name,
                "port": self.api_port,
            }
        token = self._load_cached_token()
        if token:
            try:
                self.list_secret_names()
                return {
                    "ok": True,
                    "mode": "direct",
                    "vault": self.vault_name,
                    "vault_uri": self.vault_uri,
                }
            except VaultError:
                self._vault_token = None
        if self._PENDING_DEVICE["code"]:
            if self._device_code_poll(self._PENDING_DEVICE["code"]):
                self._PENDING_DEVICE["code"] = None
                return {
                    "ok": True,
                    "mode": "direct",
                    "vault": self.vault_name,
                    "vault_uri": self.vault_uri,
                }
            return {
                "ok": False,
                "pending": True,
                "message": (
                    "Sign-in still pending; complete the device login and call "
                    "VaultConnect again."
                ),
            }
        start = self._device_code_start()
        self._PENDING_DEVICE["code"] = start.get("device_code")
        return {
            "ok": False,
            "pending": True,
            "verification_uri": start.get(
                "verification_uri", "https://microsoft.com/devicelogin"
            ),
            "user_code": start.get("user_code"),
            "message": (
                "Open the verification URI in a browser, sign in with the code "
                "shown, then call VaultConnect again to finish."
            ),
        }

    # ------------------------------------------------------------------
    # Secret operations

    def get_secret(self, name):
        if self._channel() == "companion":
            payload = self._companion_request(
                "GET",
                f"/api/keyvault/{self.vault_name}/secrets/{urllib.parse.quote(name)}",
                None,
            )
            value = payload.get("value")
            if value is None:
                raise VaultError(f"Secret '{name}' not found.")
            return str(value)
        token = self._load_cached_token()
        if not token:
            raise VaultError(
                "Direct vault access is not authorized yet. Use VaultConnect to "
                "complete the device-code sign-in first."
            )
        headers = {"Authorization": AUTH_SCHEME + " " + token}
        url = (
            f"{self.vault_uri}/secrets/{urllib.parse.quote(name)}"
            f"?api-version={VAULT_API_VERSION}"
        )
        status, body = request_http(url, "GET", None, headers, 30)
        if status == 404:
            raise VaultError(f"Secret '{name}' not found.")
        if status >= 400:
            raise VaultError(f"Vault returned HTTP {status}: {body[:200]}")
        return str(json.loads(body).get("value", ""))

    def list_secret_names(self):
        if self._channel() == "companion":
            payload = self._companion_request(
                "GET", f"/api/keyvault/{self.vault_name}/secrets", None
            )
            items = payload.get("value") or payload.get("secrets") or []
            return [str(item.get("name")) for item in items if item.get("name")]
        token = self._load_cached_token()
        if not token:
            raise VaultError(
                "Direct vault access is not authorized yet. Use VaultConnect to "
                "complete the device-code sign-in first."
            )
        headers = {"Authorization": AUTH_SCHEME + " " + token}
        names = []
        url = f"{self.vault_uri}/secrets?api-version={VAULT_API_VERSION}"
        for _page in range(10):
            status, body = request_http(url, "GET", None, headers, 30)
            if status >= 400:
                raise VaultError(f"Vault returned HTTP {status}: {body[:200]}")
            payload = json.loads(body)
            names.extend(
                str(item.get("name"))
                for item in payload.get("value", [])
                if item.get("name")
            )
            next_link = payload.get("nextLink")
            if not next_link:
                break
            url = next_link
        return names

    def set_secret(self, name, value):
        if self._channel() == "companion":
            self._companion_request(
                "PUT",
                f"/api/keyvault/{self.vault_name}/secrets/{urllib.parse.quote(name)}",
                {"value": value},
            )
            return
        token = self._load_cached_token()
        if not token:
            raise VaultError(
                "Direct vault access is not authorized yet. Use VaultConnect to "
                "complete the device-code sign-in first."
            )
        headers = {
            "Authorization": AUTH_SCHEME + " " + token,
            "Content-Type": "application/json",
        }
        url = (
            f"{self.vault_uri}/secrets/{urllib.parse.quote(name)}"
            f"?api-version={VAULT_API_VERSION}"
        )
        status, body = request_http(url, "PUT", {"value": value}, headers, 30)
        if status >= 400:
            raise VaultError(f"Vault write failed (HTTP {status}): {body[:200]}")

    # ------------------------------------------------------------------
    # Graph helpers (OTP retrieval for password resets)

    def _graph_token(self):
        password = self.get_secret(password_secret_name(self.reset_email))
        status, body = _post_form(
            f"https://login.microsoftonline.com/{self.graph_tenant}"
            "/oauth2/v2.0/token",
            {
                "grant_type": "password",
                "client_id": self.client_id,
                "username": self.reset_email,
                "password": password,
                "scope": "https://graph.microsoft.com/.default",
            },
        )
        payload = json.loads(body) if body else {}
        if status != 200 or "access_token" not in payload:
            raise VaultError(
                "Graph ROPC sign-in failed for the reset mailbox: "
                + str(payload.get("error_description", body[:200]))
            )
        return payload["access_token"]

    def fetch_otp(self, started_iso, timeout_seconds=90):
        token = self._graph_token()
        headers = {"Authorization": AUTH_SCHEME + " " + token}
        url = (
            "https://graph.microsoft.com/v1.0/me/messages"
            "?$top=25&$orderby=receivedDateTime desc"
            "&$select=subject,bodyPreview,toRecipients,receivedDateTime"
        )
        deadline = time.time() + timeout_seconds
        otp = None
        while time.time() < deadline:
            status, body = request_http(url, "GET", None, headers, 30)
            if status == 200:
                for message in json.loads(body).get("value", []):
                    received = str(message.get("receivedDateTime", ""))
                    if received < started_iso:
                        continue
                    recipients = " ".join(
                        str(addr.get("emailAddress", {}).get("address", ""))
                        for addr in message.get("toRecipients", [])
                    )
                    if self.reset_email.lower() not in recipients.lower():
                        continue
                    haystack = " ".join(
                        [str(message.get("subject", "")), str(message.get("bodyPreview", ""))]
                    )
                    match = re.search(r"\b\d{5,}\b", haystack)
                    if match:
                        otp = match.group(0)
            if otp:
                return otp
            time.sleep(5)
        raise VaultError("No OTP email arrived within the wait window.")

    # ------------------------------------------------------------------
    # Credential listing helpers

    def list_credentials(self):
        names = self.list_secret_names()
        accounts = {}
        for name in names:
            email = email_from_secret_name(name)
            if not email or "@" not in email:
                continue
            entry = accounts.setdefault(
                email,
                {"email": email, "category": classify_email(email), "has_password": False},
            )
            if name.endswith("---password"):
                entry["has_password"] = True
        return sorted(accounts.values(), key=lambda item: item["email"])

    # ------------------------------------------------------------------
    # Browser login flow

    @staticmethod
    def _flatten_controls(page):
        controls = []
        if isinstance(page, dict):
            if "controls" in page and isinstance(page["controls"], list):
                for item in page["controls"]:
                    if isinstance(item, dict) and item.get("ref"):
                        controls.append(item)
            elif "text" in page and isinstance(page["text"], dict):
                controls.extend(VaultClient._flatten_controls(page["text"]))
        return controls

    @staticmethod
    def _find_control(controls, kinds, keywords, types):
        lowered_keywords = [keyword.lower() for keyword in keywords]
        for control in controls:
            label = str(control.get("label", "")).lower()
            control_type = str(control.get("type", "")).lower()
            if control_type in types and any(
                keyword in label for keyword in lowered_keywords
            ):
                return control
        return None

    def login(self, executor, url, email, submit=False):
        executor({"tool": "Navigate", "arguments": {"url": url}})
        executor({"tool": "WAIT", "arguments": {"seconds": 2}})
        page = executor(
            {
                "tool": "ReadPage",
                "arguments": {"filter": "all", "depth": 5},
            }
        )
        controls = self._flatten_controls(page)
        email_field = self._find_control(
            controls,
            ("input",),
            ("email", "user name", "username", "sign in id", "login id", "email id"),
            ("text", "email", "tel"),
        )
        password_field = self._find_control(
            controls, ("input",), ("password",), ("password",)
        )

        if not password_field:
            # Entra-style page: submit the email first to reveal the password field.
            if not email_field:
                raise VaultError("Could not find an email or password field on the page.")
            executor(
                {
                    "tool": "FormInput",
                    "arguments": {"ref": email_field["ref"], "text": email},
                }
            )
            executor({"tool": "KEY", "arguments": {"key": "ENTER"}})
            executor({"tool": "WAIT", "arguments": {"seconds": 2}})
            page = executor(
                {"tool": "ReadPage", "arguments": {"filter": "all", "depth": 5}}
            )
            controls = self._flatten_controls(page)
            password_field = self._find_control(
                controls, ("input",), ("password",), ("password",)
            )
            if not password_field:
                raise VaultError(
                    "No password field appeared after submitting the email; the "
                    "page may use a flow the agent cannot automate."
                )

        secret_name = password_secret_name(email)
        password = self.get_secret(secret_name)
        executor(
            {
                "tool": "FormInput",
                "arguments": {"ref": password_field["ref"], "text": password},
            }
        )
        submitted = False
        if submit:
            executor({"tool": "KEY", "arguments": {"key": "ENTER"}})
            executor({"tool": "WAIT", "arguments": {"seconds": 2}})
            after = executor(
                {"tool": "ReadPage", "arguments": {"filter": "text", "depth": 3}}
            )
            text = str(after.get("text", "")) if isinstance(after, dict) else str(after)
            lowered = text.lower()
            if any(
                phrase in lowered
                for phrase in (
                    "incorrect password",
                    "wrong password",
                    "expired",
                    "invalid credentials",
                    "try again",
                )
            ):
                raise VaultError(
                    "Login rejected; password appears expired or incorrect. "
                    "Consider VaultResetPassword for this account."
                )
            submitted = True
        return {
            "ok": True,
            "email": email,
            "category": classify_email(email),
            "submitted": submitted,
        }

    # ------------------------------------------------------------------
    # Account guard (direct mode only)

    def _check_account_guard(self):
        if self._channel() != "direct":
            return
        token = self._load_cached_token()
        if not token:
            return
        headers = {"Authorization": AUTH_SCHEME + " " + token}
        status, body = request_http(
            "https://management.azure.com/tenants?api-version=2022-12-01",
            "GET",
            None,
            headers,
            20,
        )
        if status >= 400:
            raise VaultError("Vault token no longer valid for account guard check.")

    # ------------------------------------------------------------------
    # Password reset flow

    def reset_password(self, email):
        started_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        self._check_account_guard()
        status, body = request_http(
            f"{BPS_BASE}/ciam/bps/public/forgotpassword/",
            "POST",
            {"email": email},
            {"Content-Type": "application/json"},
            30,
        )
        payload = json.loads(body) if body else {}
        if status >= 400 or payload.get("result") not in (0, None, "0"):
            raise VaultError(f"forgotpassword request failed: {body[:200]}")
        time.sleep(20)
        otp = self.fetch_otp(started_iso)
        new_password = generate_password()
        status, body = request_http(
            f"{BPS_BASE}/ciam/bps/public/passwordreset/",
            "POST",
            {
                "email": email,
                "newPassword": new_password,
                "confirmNewPassword": new_password,
                "otp": otp,
            },
            {"Content-Type": "application/json"},
            30,
        )
        payload = json.loads(body) if body else {}
        if status >= 400 or payload.get("result") not in (0, None, "0"):
            raise VaultError(f"passwordreset request failed: {body[:200]}")
        self.set_secret(password_secret_name(email), new_password)
        return {
            "ok": True,
            "email": email,
            "otp_hint": mask_secret(otp),
            "password": mask_secret(new_password),
            "note": (
                "Password rotated and stored in the vault under "
                f"{password_secret_name(email)}; it was masked here on purpose."
            ),
        }


# ---------------------------------------------------------------------------
# Tool dispatch


def dispatch_tool(name, arguments, config, allow_sensitive, executor=None):
    arguments = arguments or {}
    try:
        client = VaultClient(config)
        if name == "VaultConnect":
            return client.connect()

        if name == "VaultListCredentials":
            accounts = client.list_credentials()
            return {
                "ok": True,
                "vault": client.vault_name,
                "count": len(accounts),
                "accounts": accounts,
            }

        if name == "VaultGetCredential":
            email = arguments.get("email")
            if not email:
                raise VaultError("Provide the account email to look up.")
            reveal = bool(arguments.get("reveal")) and allow_sensitive
            password = client.get_secret(password_secret_name(email))
            if not reveal:
                password = mask_secret(password)
            return {
                "ok": True,
                "email": email,
                "category": classify_email(email),
                "password": password,
                "revealed": reveal,
            }

        if name == "VaultLogin":
            if not allow_sensitive:
                raise VaultError(
                    "VaultLogin needs explicit approval: re-run with sensitive "
                    "actions allowed and submit=true to actually sign in."
                )
            url = arguments.get("url")
            email = arguments.get("email")
            if not url or not email:
                raise VaultError("Provide both the login url and account email.")
            if not executor:
                raise VaultError("No browser session is attached for VaultLogin.")
            return client.login(executor, url, email, submit=bool(arguments.get("submit")))

        if name == "VaultResetPassword":
            if not allow_sensitive or not arguments.get("confirm"):
                raise VaultError(
                    "VaultResetPassword rotates a live password; it requires "
                    "sensitive actions allowed AND confirm=true."
                )
            email = arguments.get("email") or client.reset_email
            if not email:
                raise VaultError("Provide the account email to reset.")
            return client.reset_password(email)

        raise VaultError(f"Unknown vault tool '{name}'.")
    except (VaultError, TimeoutError, ValueError) as error:
        return {"error": str(error)}
    except Exception as error:  # defensive: never crash the agent loop
        return {"error": f"Vault tool failure: {error}"}