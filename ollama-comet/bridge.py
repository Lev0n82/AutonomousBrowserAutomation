import argparse
import base64
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import queue
import re
import secrets
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import websocket
import xml.etree.ElementTree as ET
import zipfile
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse


APP_DIR = pathlib.Path(os.environ.get("LOCALAPPDATA", pathlib.Path.home())) / "OllamaComet"
CONFIG_PATH = APP_DIR / "config.json"
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_DOCUMENT_TEXT = 80000


def load_config():
    defaults = {
        "mode": "local",
        "local_endpoint": "http://127.0.0.1:11434",
        "cloud_endpoint": "https://ollama.com",
        "local_model": "granite4.1:3b",
        "cloud_model": "",
        "chat_timeout": 600,
        "enable_vision": "auto",
        "vault_uri": "",
        "vault_name": "",
        "vault_api_port": 8080,
        "vault_api_key": "",
        "vault_api_key_secret_uri": "",
        "azure_tenant_id": "",
        "azure_client_id": "",
        "graph_tenant_id": "",
        "graph_client_id": "",
        "keychain_dev_uri": "",
        "keychain_qa_uri": "",
        "reset_email": "",
        "ado_org": "",
        "ado_project": "",
        "ado_pat": "",
        "ado_grace_api": "",
        "ado_grace_token": "",
        "environments": {},
    }
    if CONFIG_PATH.exists():
        try:
            loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            defaults.update({key: value for key, value in loaded.items() if key in defaults})
            legacy_mode = loaded.get("mode", defaults["mode"])
            legacy_model = loaded.get("model")
            legacy_endpoint = loaded.get("endpoint")
            if legacy_model:
                defaults[f"{legacy_mode}_model"] = legacy_model
            if legacy_endpoint and legacy_mode == "local":
                defaults["local_endpoint"] = legacy_endpoint
        except (OSError, ValueError):
            pass
    mode = defaults["mode"] if defaults["mode"] in {"local", "cloud"} else "local"
    defaults["mode"] = mode
    defaults["endpoint"] = defaults[f"{mode}_endpoint"]
    defaults["model"] = defaults[f"{mode}_model"]
    return defaults


def save_config(config):
    APP_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")


def chat_timeout(config):
    try:
        return max(30, int(config.get("chat_timeout") or 600))
    except (TypeError, ValueError):
        return 600


def normalize_endpoint(endpoint):
    return endpoint.rstrip("/")


def target_url(endpoint, path):
    endpoint = normalize_endpoint(endpoint)
    if endpoint.endswith("/api") and path.startswith("/api/"):
        return endpoint + path[4:]
    return endpoint + path


def request_json(url, method="GET", payload=None, api_key=None, timeout=180):
    headers = {"Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        body = response.read()
        return response.status, response.headers.get("Content-Type", "application/json"), body


BROWSER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "Navigate",
            "description": "Navigate a Comet tab to a URL, or use back/forward.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "integer"},
                    "url": {"type": "string"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ReadPage",
            "description": "Read page text and referenced interactive elements before acting.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "integer"},
                    "filter": {
                        "type": "string",
                        "enum": ["viewport", "interactive", "all"],
                    },
                    "ref_id": {"type": "string"},
                    "depth": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "GetPageText",
            "description": "Get the current page as readable text.",
            "parameters": {
                "type": "object",
                "properties": {"tab_id": {"type": "integer"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "FormInput",
            "description": "Set a form control value using a reference returned by ReadPage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "integer"},
                    "ref": {"type": "string"},
                    "value": {},
                },
                "required": ["ref", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "TabsCreate",
            "description": "Create and activate a new browser tab.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "TabsList",
            "description": "List the open tabs in the active browser window.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "EvaluateJS",
            "description": (
                "Evaluate a JavaScript expression in the page and return its value as "
                "JSON. For read-only page state checks; do not use it to bypass the "
                "side-effect confirmation policy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "integer"},
                    "expression": {"type": "string"},
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ComputerBatch",
            "description": (
                "Run Comet-style browser actions. Supported action values: SCREENSHOT, WAIT, "
                "LEFT_CLICK, RIGHT_CLICK, DOUBLE_CLICK, TRIPLE_CLICK, TYPE, KEY, SCROLL, "
                "LEFT_CLICK_DRAG, and SCROLL_TO."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tab_id": {"type": "integer"},
                    "uuid": {"type": "string"},
                    "actions": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "action": {"type": "string"},
                                "coordinate": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                },
                                "start_coordinate": {
                                    "type": "array",
                                    "items": {"type": "number"},
                                },
                                "ref": {"type": "string"},
                                "text": {"type": "string"},
                                "duration": {"type": "number"},
                                "scroll_parameters": {"type": "object"},
                            },
                            "required": ["action"],
                        },
                    },
                },
                "required": ["actions"],
            },
        },
    },
]

VAULT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "VaultConnect",
            "description": (
                "Test and establish the Key Vault connection. Uses the Azure Secret "
                "Manager companion app when it is running, otherwise the direct "
                "Azure Key Vault device-code flow. Call again after the user signs "
                "in at microsoft.com/devicelogin to finish a pending connection."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "VaultListCredentials",
            "description": (
                "List the credential emails stored in the vault, grouped by "
                "category (OPS-BPS-Secure or EntraID)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["all", "OPS-BPS-Secure", "EntraID"],
                        "description": "Filter the listing. Defaults to all.",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "VaultGetCredential",
            "description": (
                "Look up the stored password for an email account. Passwords stay "
                "masked unless the user explicitly asked to reveal them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "reveal": {
                        "type": "boolean",
                        "description": (
                            "Set true only when the user explicitly asked to see "
                            "the password."
                        ),
                    },
                },
                "required": ["email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "VaultLogin",
            "description": (
                "Log into a site using the stored password for an email account. "
                "The password is never returned. Requires explicit user "
                "confirmation, and 'submit': true is required to actually submit "
                "the login form."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "email": {"type": "string"},
                    "submit": {
                        "type": "boolean",
                        "description": "Set true to submit the login form.",
                    },
                },
                "required": ["url", "email"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "VaultResetPassword",
            "description": (
                "Run the OPS-BPS secure password reset for an email account: "
                "request the reset, read the OTP from the reset mailbox, set a "
                "new password, and sync it to the vault. Requires explicit user "
                "confirmation and 'confirm': true."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "email": {"type": "string"},
                    "confirm": {
                        "type": "boolean",
                        "description": "Set true to actually perform the reset.",
                    },
                },
                "required": ["email", "confirm"],
            },
        },
    },
]

ADO_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "AdoConnect",
            "description": "Validate the Azure DevOps connection and list accessible projects. Requires ado_org and ado_pat in settings.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "AdoListRepositories",
            "description": "List git repositories in an Azure DevOps project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name; defaults to the configured ado_project."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "AdoFindTestFiles",
            "description": "Locate Excel (.xlsx) test-case workbooks checked into Azure DevOps git repositories.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "repo": {"type": "string", "description": "Restrict the search to one repository."},
                    "limit": {"type": "integer", "description": "Maximum files to return (default 25, max 100)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "AdoInspectTestFile",
            "description": "Peek inside an .xlsx workbook from a repository: sheet names, preview strings, and whether it looks like a GRACE test.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string"},
                    "path": {"type": "string", "description": "Full repository path of the workbook."},
                    "project": {"type": "string"},
                },
                "required": ["repo", "path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "AdoListTestPlans",
            "description": "List Azure Test Plans in a project.",
            "parameters": {
                "type": "object",
                "properties": {"project": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "AdoListSuites",
            "description": "List test suites inside an Azure Test Plan.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_id": {"type": "integer"},
                    "project": {"type": "string"},
                },
                "required": ["plan_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "AdoListTestPoints",
            "description": "List test points inside a test suite, with their outcomes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_id": {"type": "integer"},
                    "suite_id": {"type": "integer"},
                    "outcome": {"type": "string", "description": "Filter by outcome, e.g. Passed or NotExecuted."},
                    "project": {"type": "string"},
                },
                "required": ["plan_id", "suite_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "AdoListTestRuns",
            "description": "List recent Azure Test Plans runs (also used to observe GRACE self-reported outcomes).",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "top": {"type": "integer", "description": "Maximum runs to return (default 25)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "AdoRunGraceTest",
            "description": "Send an Excel test workbook to the GRACE API for execution. GRACE reports outcomes to Azure Test Plans itself. Requires confirm=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository of the workbook (with path), or use local_path."},
                    "path": {"type": "string", "description": "Repository path of the .xlsx workbook."},
                    "local_path": {"type": "string", "description": "Optional local workbook path instead of repo/path."},
                    "env": {"type": "string", "description": "Target environment key, e.g. EDCS-9."},
                    "browser": {"type": "string", "description": "Browser to run with, e.g. chrome or edge."},
                    "browser_version": {"type": "string"},
                    "confirm": {"type": "boolean", "description": "Set true to actually execute the test."},
                },
                "required": ["env", "browser", "confirm"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "AdoPublishTestRun",
            "description": "Create an Azure Test Plans run, publish per-test-point outcomes and close the run. Requires confirm=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "plan_id": {"type": "integer"},
                    "point_ids": {"type": "array", "items": {"type": "integer"}, "description": "Test point ids included in the run."},
                    "results": {
                        "type": "array",
                        "description": "One entry per test point: {point_id, outcome, comment?, duration_ms?, error?}.",
                        "items": {"type": "object"},
                    },
                    "name": {"type": "string", "description": "Run name; defaults to a GRACE automated run."},
                    "project": {"type": "string"},
                    "confirm": {"type": "boolean", "description": "Set true to actually publish."},
                },
                "required": ["plan_id", "point_ids", "results", "confirm"],
            },
        },
    },
]

_ADO_MODULE = {"value": None}


def load_ado_module():
    if _ADO_MODULE["value"] is not None:
        return _ADO_MODULE["value"]
    ado_path = pathlib.Path(__file__).resolve().parent / "ado.py"
    spec = importlib.util.spec_from_file_location("autonomous_browser_ado", ado_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _ADO_MODULE["value"] = module
    return module


def ado_configured(config):
    return bool(str(config.get("ado_org", "")).strip()) and bool(
        str(config.get("ado_pat", "")).strip()
    )

_VAULT_MODULE = {"value": None}


def load_vault_module():
    if _VAULT_MODULE["value"] is not None:
        return _VAULT_MODULE["value"]
    vault_path = pathlib.Path(__file__).resolve().parent / "vault.py"
    spec = importlib.util.spec_from_file_location(
        "autonomous_browser_vault", vault_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _VAULT_MODULE["value"] = module
    return module


def vault_configured(config):
    return bool(str(config.get("vault_uri", "")).strip()) and bool(
        str(config.get("vault_name", "")).strip()
    )


LAUNCHPAD_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "ListEnvironments",
            "description": (
                "List launchpad environments configured for 1-click launch "
                "(name, url, account)."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "LaunchEnvironment",
            "description": (
                "Open a configured launchpad environment and sign in with its "
                "vault credential. Reports signed_in, mfa_required, "
                "captcha_detected, password_expired, or login_form_still_present."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Configured environment name."},
                    "confirm": {
                        "type": "boolean",
                        "description": "Set true to actually open the environment and sign in.",
                    },
                },
                "required": ["name"],
            },
        },
    },
]

_LAUNCHPAD_MODULE = {"value": None}


def load_launchpad_module():
    if _LAUNCHPAD_MODULE["value"] is not None:
        return _LAUNCHPAD_MODULE["value"]
    launchpad_path = pathlib.Path(__file__).resolve().parent / "launchpad.py"
    spec = importlib.util.spec_from_file_location(
        "autonomous_browser_launchpad", launchpad_path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _LAUNCHPAD_MODULE["value"] = module
    return module

AGENT_SYSTEM_PROMPT = """You are the Ollama browser assistant embedded in Perplexity Comet.
Use the browser extension through only these tools:
Navigate, ReadPage, GetPageText, FormInput, TabsCreate, TabsList, EvaluateJS, and ComputerBatch.
Vault tools may also be available when a Key Vault is connected: VaultConnect,
VaultListCredentials, VaultGetCredential, VaultLogin, and VaultResetPassword.
ADO tools may also be available when Azure DevOps is configured: AdoConnect,
AdoListRepositories, AdoFindTestFiles, AdoInspectTestFile, AdoListTestPlans,
AdoListSuites, AdoListTestPoints, AdoListTestRuns, AdoRunGraceTest, and
AdoPublishTestRun. AdoRunGraceTest and AdoPublishTestRun need confirm=true.
Launchpad tools are also available: ListEnvironments and LaunchEnvironment.
LaunchEnvironment opens a configured environment (a saved url + account pair,
for example PR1 Production or QA Stack), signs in with the vault credential for
that account, and reports one of signed_in, mfa_required, captcha_detected,
password_expired, login_form_still_present, or preview. Set confirm=true only
after the user explicitly asks to launch that environment; the bridge requires
sensitive approval as well. When the launch reports mfa_required or
captcha_detected, hand control back to the user to finish the challenge. When
it reports password_expired, offer VaultResetPassword.
ComputerBatch action objects use the action values SCREENSHOT, WAIT, LEFT_CLICK, RIGHT_CLICK,
DOUBLE_CLICK, TRIPLE_CLICK, TYPE, KEY, SCROLL, LEFT_CLICK_DRAG, and SCROLL_TO.
Use the exact JSON schemas supplied with the tools; never invent a method or parameter.
Use these tools to complete the user's task in the active main browser section while the
conversation remains in the assistant section.
Always inspect a page with ReadPage before clicking or entering values. Prefer element references
over coordinates. Use TabsList to discover other open tabs before switching, and use EvaluateJS
only to read page state or compute values, never to bypass the confirmation rules. When a
SCREENSHOT action succeeds, the captured image is attached to the tool result automatically.
Keep the user informed in the final answer, but do not invent results.
When the user requests the main window or current tab, reuse it with Navigate instead of creating
a background tab. Browser targets are brought to the foreground; do not claim navigation unless
the corresponding browser tool returned successfully.
Do not repeat an identical tool call when the page state has not changed. After completing the
requested browser action, stop calling tools and provide the final answer immediately.

Side-effect policy:
- Navigation, reading, scrolling, screenshots, and harmless form preparation are allowed
  automatically.
- For every side effect (submitting a form, sending a message, purchasing, deleting content,
  downloading files, entering credentials, signing in, changing account state) state exactly what
  you are about to do and ask the user to confirm that specific action, for example: "I will
  click Submit on this order form. Reply 'confirm submit' to proceed."
- Proceed only when the user's latest message explicitly confirms that action; otherwise stop
  before the side effect and ask for it.
- Vault logins and password resets are additionally enforced by the bridge: VaultLogin needs
  submit=true and VaultResetPassword needs confirm=true after explicit user confirmation.
- Never display or repeat full passwords in your replies; vault credentials are masked.
- CAPTCHA and human-verification pages cannot be solved; hand control back to the user.
- Internal browser pages (chrome://, edge://, about:) and other restricted URLs may not be
  controllable. If a tool fails there, tell the user which setting to change manually."""

SENSITIVE_WORDS = {
    "confirm",
    "submit",
    "send",
    "purchase",
    "buy",
    "delete",
    "download",
    "password",
    "credential",
    "sign in",
    "log in",
    "pay",
    "order",
}


def compact_tool_result(value, key="", depth=0):
    if depth > 8:
        return "[nested value omitted]"
    normalized_key = key.lower()
    if any(
        marker in normalized_key
        for marker in ("base64", "data_url", "screenshot", "image")
    ):
        if isinstance(value, str):
            return f"[binary image omitted: {len(value)} characters]"
    if isinstance(value, dict):
        return {
            str(child_key): compact_tool_result(
                child_value,
                str(child_key),
                depth + 1,
            )
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        limited = value[:100]
        compacted = [
            compact_tool_result(item, key, depth + 1) for item in limited
        ]
        if len(value) > len(limited):
            compacted.append(f"[{len(value) - len(limited)} additional items omitted]")
        return compacted
    if isinstance(value, str):
        limit = 24000 if normalized_key in {"text", "markdown", "message"} else 8000
        if len(value) > limit:
            return value[:limit] + f"\n[truncated {len(value) - limit} characters]"
    return value


def compact_agent_messages(messages, max_characters=140000):
    def message_size(message):
        return len(json.dumps(message, ensure_ascii=False))

    total = sum(message_size(message) for message in messages)
    if total <= max_characters:
        return messages

    system_messages = [
        message for message in messages if message.get("role") == "system"
    ][:2]
    latest_user = next(
        (
            message
            for message in reversed(messages)
            if message.get("role") == "user"
        ),
        None,
    )
    recent = []
    recent_size = sum(message_size(message) for message in system_messages)
    if latest_user:
        recent_size += message_size(latest_user)
    for message in reversed(messages):
        if message in system_messages or message is latest_user:
            continue
        size = message_size(message)
        if recent_size + size > max_characters:
            break
        recent.append(message)
        recent_size += size
    recent.reverse()
    compacted = list(system_messages)
    compacted.append(
        {
            "role": "system",
            "content": (
                "Earlier browser-agent turns were removed to stay within the model context "
                "window. Continue from the recent native tool results without repeating "
                "completed actions."
            ),
        }
    )
    compacted.extend(recent)
    if latest_user and latest_user not in compacted:
        compacted.append(latest_user)
    return compacted


VISION_CAPABILITY_CACHE = {}
_IMAGE_KEY_MARKERS = ("base64", "data_url", "screenshot", "image")


def extract_images(value, key="", depth=0):
    """Collect base64 image data from a raw tool result before compaction."""
    if depth > 8:
        return []
    normalized_key = key.lower()
    if any(marker in normalized_key for marker in _IMAGE_KEY_MARKERS):
        if isinstance(value, str) and len(value) > 256:
            data = value
            prefix = re.match(r"^data:[^;]+;base64,", value)
            if prefix:
                data = value[prefix.end():]
            return [data]
    if isinstance(value, dict):
        images = []
        for child_key, child_value in value.items():
            images.extend(extract_images(child_value, str(child_key), depth + 1))
        return images
    if isinstance(value, list):
        images = []
        for item in value[:100]:
            images.extend(extract_images(item, key, depth + 1))
        return images
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("{", "[") and len(stripped) < 200000:
            try:
                return extract_images(json.loads(stripped), key, depth + 1)
            except ValueError:
                return []
    return []


def model_supports_vision(endpoint, model):
    cache_key = (normalize_endpoint(str(endpoint)), str(model))
    if cache_key in VISION_CAPABILITY_CACHE:
        return VISION_CAPABILITY_CACHE[cache_key]
    supported = False
    try:
        _, _, body = request_json(
            target_url(endpoint, "/api/show"),
            method="POST",
            payload={"model": model},
            timeout=30,
        )
        info = json.loads(body)
        supported = "vision" in (info.get("capabilities") or [])
        if not supported:
            model_info_blob = json.dumps(info.get("model_info") or {}).lower()
            supported = any(
                marker in model_info_blob
                for marker in ("vision", "mmproj", "clip-vit", "llava")
            )
    except (OSError, ValueError, KeyError):
        supported = False
    VISION_CAPABILITY_CACHE[cache_key] = supported
    return supported


def collect_tool_images(config, model, raw_result):
    mode = str(config.get("enable_vision", "auto")).strip().lower()
    if mode in {"false", "0", "no", "off", "disabled"}:
        return []
    images = extract_images(raw_result)
    if not images:
        return []
    if mode == "true":
        return images[:4]
    if not model_supports_vision(config.get("endpoint", ""), model):
        return []
    return images[:4]


def decode_attachment(attachment):
    name = pathlib.Path(str(attachment.get("name", "attachment"))).name
    encoded = str(attachment.get("data", ""))
    if not encoded:
        raise ValueError(f"{name}: attachment data is empty")
    try:
        content = base64.b64decode(encoded, validate=True)
    except ValueError as error:
        raise ValueError(f"{name}: invalid base64 attachment") from error
    if len(content) > MAX_ATTACHMENT_BYTES:
        raise ValueError(f"{name}: attachment exceeds the 20 MB limit")
    return name, str(attachment.get("type", "")), content


def extract_docx(content):
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if sum(item.file_size for item in archive.infolist()) > 100 * 1024 * 1024:
            raise ValueError("DOCX expanded content exceeds the 100 MB safety limit")
        document = ET.fromstring(archive.read("word/document.xml"))
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    paragraphs = []
    for paragraph in document.findall(".//w:p", namespace):
        text = "".join(
            node.text or "" for node in paragraph.findall(".//w:t", namespace)
        ).strip()
        if text:
            paragraphs.append(text)
    return "\n".join(paragraphs)


def extract_xlsx(content):
    namespace = {"x": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    relationship_namespace = {
        "r": "http://schemas.openxmlformats.org/package/2006/relationships"
    }
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        if sum(item.file_size for item in archive.infolist()) > 100 * 1024 * 1024:
            raise ValueError("XLSX expanded content exceeds the 100 MB safety limit")
        shared_strings = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            for item in root.findall("x:si", namespace):
                shared_strings.append(
                    "".join(
                        node.text or "" for node in item.findall(".//x:t", namespace)
                    )
                )

        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        relationships = ET.fromstring(
            archive.read("xl/_rels/workbook.xml.rels")
        )
        relationship_targets = {
            item.attrib["Id"]: item.attrib["Target"]
            for item in relationships.findall("r:Relationship", relationship_namespace)
        }
        rows = []
        for sheet in workbook.findall(".//x:sheet", namespace):
            name = sheet.attrib.get("name", "Sheet")
            relationship_id = sheet.attrib.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
            )
            target = relationship_targets.get(relationship_id, "")
            clean_target = target.lstrip("/")
            sheet_path = (
                clean_target if clean_target.startswith("xl/") else "xl/" + clean_target
            )
            if sheet_path not in archive.namelist():
                continue
            rows.append(f"## Sheet: {name}")
            sheet_root = ET.fromstring(archive.read(sheet_path))
            for row in sheet_root.findall(".//x:row", namespace):
                values = []
                for cell in row.findall("x:c", namespace):
                    value_node = cell.find("x:v", namespace)
                    value = value_node.text if value_node is not None else ""
                    if cell.attrib.get("t") == "s" and value.isdigit():
                        index = int(value)
                        value = (
                            shared_strings[index]
                            if index < len(shared_strings)
                            else value
                        )
                    elif cell.attrib.get("t") == "inlineStr":
                        value = "".join(
                            node.text or ""
                            for node in cell.findall(".//x:t", namespace)
                        )
                    values.append(value)
                if values:
                    rows.append("\t".join(values))
                if sum(len(item) for item in rows) >= MAX_DOCUMENT_TEXT:
                    break
    return "\n".join(rows)


def extract_document(name, mime_type, content):
    suffix = pathlib.Path(name).suffix.lower()
    if suffix == ".pdf" or mime_type == "application/pdf":
        try:
            from pypdf import PdfReader
        except ImportError as error:
            raise RuntimeError(
                "PDF support is not installed. Re-run Install-OllamaComet.ps1."
            ) from error
        reader = PdfReader(io.BytesIO(content))
        pages = []
        for index, page in enumerate(reader.pages, start=1):
            pages.append(f"## Page {index}\n{page.extract_text() or ''}")
            if sum(len(item) for item in pages) >= MAX_DOCUMENT_TEXT:
                break
        text = "\n\n".join(pages)
    elif suffix == ".docx":
        text = extract_docx(content)
    elif suffix == ".xlsx":
        text = extract_xlsx(content)
    else:
        raise ValueError(
            f"{name}: unsupported document type; use PDF, DOCX, or XLSX"
        )
    if len(text) > MAX_DOCUMENT_TEXT:
        text = text[:MAX_DOCUMENT_TEXT] + "\n[document text truncated]"
    return text.strip()


def prepare_supplied_message(message):
    prepared = {
        "role": message.get("role"),
        "content": str(message.get("content", "")),
    }
    images = list(message.get("images") or [])
    document_sections = []
    for attachment in message.get("attachments") or []:
        name, mime_type, content = decode_attachment(attachment)
        if mime_type.startswith("image/"):
            images.append(base64.b64encode(content).decode("ascii"))
            continue
        document_text = extract_document(name, mime_type, content)
        document_sections.append(
            f"\n\n--- Attached document: {name} ---\n{document_text}"
        )
    if document_sections:
        prepared["content"] += "".join(document_sections)
    if images:
        prepared["images"] = images[:6]
    return prepared


class BrowserBroker:
    def __init__(self):
        self.commands = queue.Queue()
        self.pending = {}
        self.lock = threading.Lock()
        self.last_seen = 0.0

    def next_command(self, timeout=25):
        self.last_seen = time.monotonic()
        try:
            return self.commands.get(timeout=timeout)
        except queue.Empty:
            return None

    def complete(self, command_id, result):
        with self.lock:
            pending = self.pending.get(command_id)
            if pending:
                pending["result"] = result
                pending["event"].set()

    def execute(self, method, arguments, allow_sensitive=False, timeout=45):
        command_id = str(uuid.uuid4())
        pending = {"event": threading.Event(), "result": None}
        with self.lock:
            self.pending[command_id] = pending
        self.commands.put(
            {
                "id": command_id,
                "method": method,
                "arguments": arguments,
                "allow_sensitive": allow_sensitive,
            }
        )
        try:
            if not pending["event"].wait(timeout):
                raise TimeoutError(
                    "The browser-control extension did not return a result. "
                    "Confirm that the extension is enabled and connected to this bridge."
                )
            result = pending["result"] or {}
            if not result.get("ok"):
                raise RuntimeError(result.get("error") or f"{method} failed")
            return result.get("value")
        finally:
            with self.lock:
                self.pending.pop(command_id, None)

    def connected(self):
        return time.monotonic() - self.last_seen < 35


class AgentTaskRegistry:
    def __init__(self):
        self.tasks = {}
        self.lock = threading.Lock()

    def create(self, task_id):
        entry = {"cancel": threading.Event(), "events": []}
        with self.lock:
            self.tasks[task_id] = entry
        return entry["cancel"]

    def cancel(self, task_id):
        with self.lock:
            entry = self.tasks.get(task_id)
        if not entry:
            return False
        entry["cancel"].set()
        return True

    def remove(self, task_id):
        with self.lock:
            self.tasks.pop(task_id, None)

    def log(self, task_id, event):
        with self.lock:
            entry = self.tasks.get(task_id)
            if entry is not None:
                entry["events"].append(event)

    def progress(self, task_id, after=0):
        with self.lock:
            entry = self.tasks.get(task_id)
            if entry is None:
                return {"task_id": task_id, "active": False, "index": 0, "events": []}
            return {
                "task_id": task_id,
                "active": True,
                "index": len(entry["events"]),
                "events": list(entry["events"][max(0, after):]),
            }


class NativeWebSocketConnection:
    def __init__(self, handler):
        self.rfile = handler.rfile
        self.wfile = handler.wfile
        self.send_lock = threading.Lock()
        self.closed = False

    def _read_exact(self, length):
        chunks = []
        remaining = length
        while remaining:
            chunk = self.rfile.read(remaining)
            if not chunk:
                raise EOFError("WebSocket connection closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read_frame(self):
        first, second = self._read_exact(2)
        finished = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._read_exact(8))[0]
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length) if length else b""
        if masked:
            payload = bytes(
                value ^ mask[index % 4] for index, value in enumerate(payload)
            )
        return finished, opcode, payload

    def send_frame(self, opcode, payload=b""):
        if self.closed:
            raise RuntimeError("Native Comet agent WebSocket is closed")
        payload = payload if isinstance(payload, bytes) else payload.encode("utf-8")
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | opcode, length])
        elif length <= 0xFFFF:
            header = bytes([0x80 | opcode, 126]) + struct.pack("!H", length)
        else:
            header = bytes([0x80 | opcode, 127]) + struct.pack("!Q", length)
        with self.send_lock:
            self.wfile.write(header + payload)
            self.wfile.flush()

    def send_json(self, value):
        self.send_frame(0x1, json.dumps(value))

    def close(self):
        if self.closed:
            return
        try:
            self.send_frame(0x8, struct.pack("!H", 1000))
        except (OSError, RuntimeError):
            pass
        self.closed = True


class NativeAgentSession:
    def __init__(self, hub, task_id, cancel_event):
        self.hub = hub
        self.task_id = task_id
        self.cancel_event = cancel_event
        self.connection = None
        self.start_payload = None
        self.ready = threading.Event()
        self.failure = None

    def attach(self, connection, start_payload):
        self.connection = connection
        self.start_payload = start_payload
        self.ready.set()

    def fail(self, error):
        self.failure = str(error)
        self.ready.set()

    def wait_ready(self, timeout=20):
        deadline = time.monotonic() + timeout
        while not self.ready.wait(0.1):
            if self.cancel_event.is_set():
                raise RuntimeError("Native Comet agent startup was cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "The signed Comet Agent extension did not connect to /agent."
                )
        if self.failure:
            raise RuntimeError(self.failure)
        return self.start_payload

    def rpc(self, method, arguments):
        if not self.connection or self.connection.closed:
            raise RuntimeError("Native Comet agent WebSocket is not connected")
        request_id = str(uuid.uuid4())
        pending = self.hub.register_request(request_id, self)
        self.connection.send_json(
            {
                "task_uuid": self.task_id,
                "request_id": request_id,
                "method": method,
                "request": json.dumps(arguments),
            }
        )
        try:
            while not pending["event"].wait(0.1):
                if self.cancel_event.is_set():
                    raise RuntimeError("Native Comet agent task was cancelled")
                if self.failure:
                    raise RuntimeError(self.failure)
            response = pending.get("response") or {}
            serialized = response.get("response", "{}")
            return json.loads(serialized) if serialized else {}
        finally:
            self.hub.remove_request(request_id)

    def complete(self, message):
        if self.connection and not self.connection.closed:
            self.connection.send_json(
                {
                    "task_uuid": self.task_id,
                    "request_id": str(uuid.uuid4()),
                    "method": "ReportTaskComplete",
                    "request": json.dumps({"message": message}),
                }
            )


class NativeAgentHub:
    def __init__(self):
        self.sessions = {}
        self.pending = {}
        self.lock = threading.Lock()

    def create_session(self, task_id, cancel_event):
        session = NativeAgentSession(self, task_id, cancel_event)
        with self.lock:
            self.sessions[task_id] = session
        return session

    def remove_session(self, task_id):
        with self.lock:
            self.sessions.pop(task_id, None)

    def register_request(self, request_id, session):
        pending = {"event": threading.Event(), "response": None, "session": session}
        with self.lock:
            self.pending[request_id] = pending
        return pending

    def remove_request(self, request_id):
        with self.lock:
            self.pending.pop(request_id, None)

    def handle_message(self, connection, message):
        if "start_agent" in message:
            payload = message["start_agent"]
            task_id = payload.get("task_uuid")
            with self.lock:
                session = self.sessions.get(task_id)
            if session:
                session.attach(connection, payload)
            return
        if "rpc_response" in message:
            response = message["rpc_response"]
            request_id = response.get("request_id")
            with self.lock:
                pending = self.pending.get(request_id)
            if pending:
                pending["response"] = response
                pending["event"].set()
            return
        if "stop_agent" in message:
            task_id = message["stop_agent"].get("task_uuid")
            with self.lock:
                session = self.sessions.get(task_id)
            if session:
                session.fail("The native Comet agent stopped the task")

    def disconnect(self, connection):
        with self.lock:
            sessions = [
                session
                for session in self.sessions.values()
                if session.connection is connection
            ]
        for session in sessions:
            session.fail("The native Comet Agent WebSocket disconnected")


class CDPBrowserController:
    def __init__(self, port=9223):
        self.base_url = f"http://127.0.0.1:{port}"
        self.tab_ids = {}
        self.next_tab_id = 1

    def _json(self, path, method="GET", timeout=5):
        request = urllib.request.Request(self.base_url + path, method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())

    def _pages(self):
        pages = [
            target
            for target in self._json("/json/list")
            if target.get("type") == "page"
        ]
        for page in pages:
            target_id = page["id"]
            if target_id not in self.tab_ids:
                self.tab_ids[target_id] = self.next_tab_id
                self.next_tab_id += 1
        return pages

    def _call(self, target, method, params=None, timeout=20):
        connection = websocket.create_connection(
            target["webSocketDebuggerUrl"],
            timeout=timeout,
            suppress_origin=True,
        )
        request_id = 1
        try:
            connection.send(
                json.dumps(
                    {
                        "id": request_id,
                        "method": method,
                        "params": params or {},
                    }
                )
            )
            while True:
                message = json.loads(connection.recv())
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise RuntimeError(message["error"].get("message", str(message["error"])))
                return message.get("result", {})
        finally:
            connection.close()

    def _evaluate(self, target, expression):
        result = self._call(
            target,
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(
                result["exceptionDetails"].get("text", "Page script failed")
            )
        return result.get("result", {}).get("value")

    def _is_assistant_target(self, target):
        parsed = urlparse(target.get("url", ""))
        return (
            parsed.hostname == "127.0.0.1"
            and parsed.port == 11435
            and parsed.path in {"/", "/sidecar", "/sidecar/"}
        )

    def _is_bootstrap_target(self, target):
        return "ollama_comet_native_bootstrap" in target.get("url", "")

    def _is_main_target(self, target):
        return not self._is_assistant_target(target) and not self._is_bootstrap_target(target)

    def _activate(self, target):
        version = self._json("/json/version")
        browser = {"webSocketDebuggerUrl": version["webSocketDebuggerUrl"]}
        self._call(
            browser,
            "Target.activateTarget",
            {"targetId": target["id"]},
        )
        try:
            self._call(target, "Page.bringToFront")
        except (RuntimeError, websocket.WebSocketException):
            pass
        return target

    def _target(self, requested_tab_id=None):
        pages = self._pages()
        if requested_tab_id is not None:
            normalized_tab_id = (
                int(requested_tab_id)
                if str(requested_tab_id).isdigit()
                else requested_tab_id
            )
            for page in pages:
                if (
                    page["id"] == str(requested_tab_id)
                    or self.tab_ids.get(page["id"]) == normalized_tab_id
                ):
                    if self._is_assistant_target(page):
                        raise ValueError(
                            "The Ollama assistant tab is not a controllable main-browser target."
                        )
                    if self._is_bootstrap_target(page):
                        raise ValueError(
                            "The native-agent bootstrap tab is not a controllable browser target."
                        )
                    return self._activate(page)
            raise ValueError(f"Unknown tab_id: {requested_tab_id}")

        candidates = [
            page
            for page in pages
            if self._is_main_target(page)
        ]
        for page in candidates:
            try:
                if self._evaluate(page, "document.hasFocus()"):
                    return self._activate(page)
            except (RuntimeError, OSError, websocket.WebSocketException):
                continue
        if not candidates:
            raise RuntimeError("Comet has no controllable page target.")
        return self._activate(candidates[0])

    def _context(self, executed_target=None):
        pages = [
            page for page in self._pages() if self._is_main_target(page)
        ]
        available = [
            {
                "tab_id": self.tab_ids[page["id"]],
                "title": page.get("title", ""),
                "url": page.get("url", ""),
            }
            for page in pages
        ]
        return {
            "current_tab_id": (
                self.tab_ids.get(executed_target["id"], -1) if executed_target else -1
            ),
            "executed_on_tab_id": (
                self.tab_ids.get(executed_target["id"], -1) if executed_target else -1
            ),
            "available_tabs": available,
            "tab_count": len(available),
        }

    def connected(self):
        try:
            self._json("/json/version", timeout=0.1)
            return True
        except (OSError, urllib.error.URLError, ValueError):
            return False

    def _browser_target(self):
        version = self._json("/json/version")
        return {"webSocketDebuggerUrl": version["webSocketDebuggerUrl"]}

    def _wait_external_runtime(self, target, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self._evaluate(
                    target,
                    "typeof chrome?.runtime?.sendMessage === 'function'",
                ):
                    return
            except (RuntimeError, websocket.WebSocketException):
                pass
            time.sleep(0.2)
        raise TimeoutError(
            "Comet did not expose external extension messaging on the bootstrap page."
        )

    def start_native_agent(self, task_id, task, base_url):
        main_target = self._target()
        main_url = main_target.get("url") or "https://www.google.com/"
        browser = self._browser_target()
        created = self._call(
            browser,
            "Target.createTarget",
            {
                "url": (
                    "https://www.perplexity.ai/"
                    f"?ollama_comet_native_bootstrap={task_id}"
                ),
                "background": True,
            },
        )
        bootstrap_id = created["targetId"]
        bootstrap = next(
            target for target in self._pages() if target["id"] == bootstrap_id
        )
        self._wait_ready(bootstrap, 30)
        self._wait_external_runtime(bootstrap)
        request_id = str(uuid.uuid4())
        extension_id = "npclhjbddhklpbnacpjloidibaggcgon"
        extra_headers = json.dumps(
            {
                "source": "ollama_comet",
                "enable_reconnect": False,
                "skip_sidecar": False,
            }
        )
        expression = f"""(async () => {{
          const extensionId = {json.dumps(extension_id)};
          const send = message => new Promise((resolve, reject) => {{
            chrome.runtime.sendMessage(extensionId, message, response => {{
              const error = chrome.runtime.lastError?.message;
              if (error) reject(new Error(error)); else resolve(response);
            }});
          }});
          const opened = await send({{
            type: "CALL_TOOL",
            method: "OpenTab",
            request: {{
              url: {json.dumps(main_url)},
              request_id: {json.dumps(request_id)},
              key: {json.dumps(request_id)}
            }}
          }});
          if (!opened?.success || !opened?.response?.tab?.tab_id)
            throw new Error("Comet OpenTab did not return a native tab ID.");
          const tabId = opened.response.tab.tab_id;
          const started = await send({{
            type: "START_AGENT",
            task: {json.dumps(task)},
            uuid: {json.dumps(task_id)},
            entryId: {json.dumps(task_id)},
            base_url: {json.dumps(base_url)},
            extra_headers: {json.dumps(extra_headers)},
            tab_id: tabId,
            url: opened.response.tab.url || {json.dumps(main_url)}
          }});
          const sidecar = await send({{ type: "COMET_OPEN_SIDECAR" }});
          return {{ tabId, started, sidecar }};
        }})()"""
        result = self._evaluate(bootstrap, expression)
        if not result or not result.get("started", {}).get("success"):
            raise RuntimeError(f"Native Comet START_AGENT failed: {result}")
        return bootstrap_id

    def close_target(self, target_id):
        if not target_id:
            return
        try:
            self._call(
                self._browser_target(),
                "Target.closeTarget",
                {"targetId": target_id},
            )
        except (RuntimeError, websocket.WebSocketException, urllib.error.URLError):
            pass

    def _wait_ready(self, target, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self._evaluate(target, "document.readyState") == "complete":
                    return
            except (RuntimeError, websocket.WebSocketException):
                pass
            time.sleep(0.2)
        raise TimeoutError("Timed out waiting for the page to load.")

    def _read_page(self, target, arguments):
        page_filter = json.dumps(str(arguments.get("filter", "viewport")).lower())
        depth = max(1, min(int(arguments.get("depth", 4)), 8))
        expression = f"""(() => {{
          const requestedFilter = {page_filter};
          const visible = element => {{
            const style = getComputedStyle(element);
            const rectangle = element.getBoundingClientRect();
            return style.visibility !== "hidden" && style.display !== "none" &&
              rectangle.width > 0 && rectangle.height > 0;
          }};
          const inViewport = element => {{
            const rectangle = element.getBoundingClientRect();
            return rectangle.bottom >= 0 && rectangle.right >= 0 &&
              rectangle.top <= innerHeight && rectangle.left <= innerWidth;
          }};
          const selector = [
            "a[href]", "button", "input", "textarea", "select",
            "[role=button]", "[role=link]", "[role=checkbox]", "[role=radio]",
            "[contenteditable=true]"
          ].join(",");
          const elements = [...document.querySelectorAll(selector)]
            .filter(visible)
            .filter(element => requestedFilter !== "viewport" || inViewport(element))
            .slice(0, 250);
          const interactive = elements.map((element, index) => {{
            let ref = element.getAttribute("data-ollama-comet-ref");
            if (!ref) {{
              ref = `ref_${{Date.now().toString(36)}}_${{index}}`;
              element.setAttribute("data-ollama-comet-ref", ref);
            }}
            const rectangle = element.getBoundingClientRect();
            const label = (
              element.getAttribute("aria-label") ||
              element.innerText ||
              element.getAttribute("placeholder") ||
              element.getAttribute("title") ||
              element.getAttribute("name") ||
              ""
            ).trim().replace(/\\s+/g, " ").slice(0, 240);
            return {{
              ref,
              role: element.getAttribute("role") || element.tagName.toLowerCase(),
              label,
              value: element instanceof HTMLInputElement && element.type !== "password"
                ? element.value.slice(0, 120) : "",
              disabled: Boolean(element.disabled),
              coordinate: [
                Math.round(rectangle.left + rectangle.width / 2),
                Math.round(rectangle.top + rectangle.height / 2)
              ]
            }};
          }});
          return {{
            title: document.title,
            url: location.href,
            text: (document.body?.innerText || "").trim().slice(0, {depth * 10000}),
            interactive_elements: interactive
          }};
        }})()"""
        return {
            "tab_context": self._context(target),
            "result": self._evaluate(target, expression),
        }

    def _form_input(self, target, arguments, allow_sensitive):
        ref = json.dumps(str(arguments.get("ref", "")))
        value = json.dumps(arguments.get("value", ""))
        allowed = "true" if allow_sensitive else "false"
        expression = f"""(() => {{
          const element = document.querySelector(
            `[data-ollama-comet-ref="${{CSS.escape({ref})}}"]`
          );
          if (!element) throw new Error("Referenced element was not found.");
          const label = `${{element.getAttribute("aria-label") || ""}} ${{element.name || ""}}`;
          if (!{allowed} && (
            (element instanceof HTMLInputElement && element.type === "password") ||
            /password|credential|card number|security code/i.test(label)
          )) throw new Error("Sensitive input requires explicit user confirmation.");
          const value = {value};
          element.focus({{ preventScroll: false }});
          if (element instanceof HTMLSelectElement) {{
            const choices = String(value).split(",").map(item => item.trim());
            for (const option of element.options)
              option.selected = choices.includes(option.value) || choices.includes(option.text);
          }} else if (element instanceof HTMLInputElement &&
            ["checkbox", "radio"].includes(element.type)) {{
            element.checked = element.type === "radio" || String(value).toLowerCase() === "true";
          }} else {{
            const prototype = element instanceof HTMLTextAreaElement
              ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
            if (setter) setter.call(element, String(value)); else element.value = String(value);
          }}
          element.dispatchEvent(new Event("input", {{ bubbles: true, composed: true }}));
          element.dispatchEvent(new Event("change", {{ bubbles: true }}));
          return `Input set to "${{value}}"`;
        }})()"""
        return {
            "tab_context": self._context(target),
            "message": self._evaluate(target, expression),
        }

    def _click(self, target, action, allow_sensitive):
        action_name = str(action.get("action", "LEFT_CLICK")).upper()
        sensitive = (
            "submit|send|delete|purchase|buy|download|pay|order|sign in|log in"
        )
        if action.get("ref"):
            ref = json.dumps(str(action["ref"]))
            allowed = "true" if allow_sensitive else "false"
            expression = f"""(() => {{
              const element = document.querySelector(
                `[data-ollama-comet-ref="${{CSS.escape({ref})}}"]`
              );
              if (!element) throw new Error("Referenced element was not found.");
              const label = `${{element.innerText || ""}} ${{element.getAttribute("aria-label") || ""}}`.trim();
              if (!{allowed} && /{sensitive}/i.test(label))
                throw new Error(`"${{label}}" requires explicit user confirmation.`);
              element.scrollIntoView({{ block: "center", inline: "center" }});
              if ({json.dumps(action_name)} === "RIGHT_CLICK")
                element.dispatchEvent(new MouseEvent("contextmenu", {{ bubbles: true, button: 2 }}));
              else {{
                const count = {json.dumps(action_name)} === "DOUBLE_CLICK" ? 2 :
                  {json.dumps(action_name)} === "TRIPLE_CLICK" ? 3 : 1;
                for (let index = 0; index < count; index++) element.click();
              }}
              return `${{{json.dumps(action_name)}}} on ${{{ref}}}`;
            }})()"""
            return self._evaluate(target, expression)

        coordinate = action.get("coordinate")
        if not isinstance(coordinate, list) or len(coordinate) < 2:
            raise ValueError(f"{action_name} requires ref or coordinate")
        x, y = coordinate[:2]
        label = self._evaluate(
            target,
            f"""(() => {{
              const element = document.elementFromPoint({float(x)}, {float(y)});
              return element ? `${{element.innerText || ""}} ${{element.getAttribute("aria-label") || ""}}`.trim().slice(0, 160) : "";
            }})()""",
        )
        if not allow_sensitive and label and any(
            word in label.lower() for word in sensitive.split("|")
        ):
            raise RuntimeError(f'"{label}" requires explicit user confirmation.')
        button = "right" if action_name == "RIGHT_CLICK" else "left"
        click_count = 2 if action_name == "DOUBLE_CLICK" else 3 if action_name == "TRIPLE_CLICK" else 1
        self._call(
            target,
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": button, "clickCount": click_count},
        )
        self._call(
            target,
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": button, "clickCount": click_count},
        )
        return f"{action_name} at {x},{y}"

    def _computer_batch(self, target, arguments, allow_sensitive):
        results = []
        for action in arguments.get("actions") or []:
            name = str(action.get("action", "")).upper()
            if name in {"LEFT_CLICK", "RIGHT_CLICK", "DOUBLE_CLICK", "TRIPLE_CLICK"}:
                results.append(self._click(target, action, allow_sensitive))
            elif name == "TYPE":
                self._call(target, "Input.insertText", {"text": str(action.get("text", ""))})
                results.append("Typed text")
            elif name == "KEY":
                text = str(action.get("text", "")).upper()
                keys = {
                    "ENTER": ("Enter", "Enter", 13),
                    "TAB": ("Tab", "Tab", 9),
                    "ESCAPE": ("Escape", "Escape", 27),
                    "BACKSPACE": ("Backspace", "Backspace", 8),
                    "DELETE": ("Delete", "Delete", 46),
                    "ARROWUP": ("ArrowUp", "ArrowUp", 38),
                    "ARROWDOWN": ("ArrowDown", "ArrowDown", 40),
                    "ARROWLEFT": ("ArrowLeft", "ArrowLeft", 37),
                    "ARROWRIGHT": ("ArrowRight", "ArrowRight", 39),
                }
                if text not in keys:
                    raise ValueError(f"Unsupported KEY value: {text}")
                key, code, virtual_key = keys[text]
                for event_type in ("keyDown", "keyUp"):
                    self._call(
                        target,
                        "Input.dispatchKeyEvent",
                        {
                            "type": event_type,
                            "key": key,
                            "code": code,
                            "windowsVirtualKeyCode": virtual_key,
                        },
                    )
                results.append(f"Pressed {text}")
            elif name == "WAIT":
                duration = min(max(float(action.get("duration", 1)), 0), 30)
                time.sleep(duration)
                results.append(f"Waited {duration} second(s)")
            elif name == "SCROLL":
                parameters = action.get("scroll_parameters") or {}
                direction = str(parameters.get("scroll_direction", "DOWN")).upper()
                amount = float(
                    parameters.get("viewports_to_scroll")
                    or parameters.get("scroll_amount")
                    or 1
                )
                self._evaluate(
                    target,
                    f"""(() => {{
                      const vertical = {json.dumps(direction)} === "UP" || {json.dumps(direction)} === "DOWN";
                      const sign = {json.dumps(direction)} === "UP" || {json.dumps(direction)} === "LEFT" ? -1 : 1;
                      window.scrollBy({{
                        top: vertical ? sign * innerHeight * {amount} : 0,
                        left: vertical ? 0 : sign * innerWidth * {amount},
                        behavior: "smooth"
                      }});
                      return "Scrolled {direction}";
                    }})()""",
                )
                results.append(f"Scrolled {direction}")
            elif name == "SCROLL_TO":
                ref = json.dumps(str(action.get("ref", "")))
                results.append(
                    self._evaluate(
                        target,
                        f"""(() => {{
                          const element = document.querySelector(
                            `[data-ollama-comet-ref="${{CSS.escape({ref})}}"]`
                          );
                          if (!element) throw new Error("Referenced element was not found.");
                          element.scrollIntoView({{ block: "center", inline: "center", behavior: "smooth" }});
                          return "Scrolled to element";
                        }})()""",
                    )
                )
            elif name == "LEFT_CLICK_DRAG":
                start = action.get("start_coordinate") or []
                end = action.get("coordinate") or []
                if len(start) < 2 or len(end) < 2:
                    raise ValueError("LEFT_CLICK_DRAG requires start_coordinate and coordinate")
                self._call(
                    target,
                    "Input.dispatchMouseEvent",
                    {"type": "mousePressed", "x": start[0], "y": start[1], "button": "left", "clickCount": 1},
                )
                self._call(
                    target,
                    "Input.dispatchMouseEvent",
                    {"type": "mouseMoved", "x": end[0], "y": end[1], "button": "left"},
                )
                self._call(
                    target,
                    "Input.dispatchMouseEvent",
                    {"type": "mouseReleased", "x": end[0], "y": end[1], "button": "left", "clickCount": 1},
                )
                results.append("Drag completed")
            elif name == "SCREENSHOT":
                screenshot = self._call(target, "Page.captureScreenshot", {"format": "png"})
                results.append({"base64_image": screenshot.get("data", "")})
            else:
                raise ValueError(f"Unsupported ComputerBatch action: {name}")
        return {
            "tab_context": self._context(target),
            "message": json.dumps(results),
        }

    def execute(self, method, arguments, allow_sensitive=False):
        if method == "TabsCreate":
            version = self._json("/json/version")
            browser = {
                "webSocketDebuggerUrl": version["webSocketDebuggerUrl"],
            }
            result = self._call(
                browser,
                "Target.createTarget",
                {"url": arguments.get("url") or "about:blank"},
            )
            target = self._target(result["targetId"])
            if arguments.get("url"):
                self._wait_ready(target)
            return {
                "tab_context": self._context(target),
                "message": f"Created new tab. Tab ID: {self.tab_ids[target['id']]}",
                "tab_id": self.tab_ids[target["id"]],
            }

        target = self._target(arguments.get("tab_id"))
        if method == "Navigate":
            destination = str(arguments.get("url", ""))
            if not destination:
                raise ValueError("url is required")
            if destination in {"back", "forward"}:
                delta = -1 if destination == "back" else 1
                history = self._call(target, "Page.getNavigationHistory")
                entry_id = history["entries"][history["currentIndex"] + delta]["id"]
                self._call(target, "Page.navigateToHistoryEntry", {"entryId": entry_id})
            else:
                if "://" not in destination:
                    destination = "https://" + destination
                self._call(target, "Page.navigate", {"url": destination})
            self._wait_ready(target)
            return {
                "tab_context": self._context(target),
                "message": f"Navigated to {destination}",
            }
        if method == "ReadPage":
            return self._read_page(target, arguments)
        if method == "GetPageText":
            markdown = self._evaluate(
                target,
                """(() => {
                  const title = document.title ? `# ${document.title}\n\n` : "";
                  return `${title}${document.body?.innerText || ""}`.trim().slice(0, 60000);
                })()""",
            )
            return {"tab_context": self._context(target), "markdown": markdown}
        if method == "FormInput":
            return self._form_input(target, arguments, allow_sensitive)
        if method == "ComputerBatch":
            return self._computer_batch(target, arguments, allow_sensitive)
        if method == "TabsList":
            return {"tab_context": self._context(None)}
        if method == "EvaluateJS":
            expression = arguments.get("expression")
            if not isinstance(expression, str) or not expression.strip():
                raise ValueError("expression is required")
            return {
                "tab_context": self._context(target),
                "result": self._evaluate(target, expression),
            }
        raise ValueError(f"Unsupported browser method: {method}")


def sensitive_allowed(messages):
    latest = next(
        (
            message.get("content", "")
            for message in reversed(messages)
            if message.get("role") == "user"
        ),
        "",
    )
    normalized = str(latest).lower()
    return any(word in normalized for word in SENSITIVE_WORDS)


def should_use_browser_extension(server):
    return (
        server.browser_target in {
            "chrome",
            "chromium",
            "edge",
            "firefox",
            "extension",
        }
        and server.browser_broker.connected()
    )


def run_agent(server, payload):
    config = load_config()
    model = payload.get("model") or config["model"]
    supplied_messages = payload.get("messages") or []
    messages = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
    for message in supplied_messages:
        if message.get("role") not in {"user", "assistant"}:
            continue
        if not (
            message.get("content")
            or message.get("images")
            or message.get("attachments")
        ):
            continue
        messages.append(prepare_supplied_message(message))
    allow_sensitive = sensitive_allowed(messages)
    trace = []
    tool_names = {tool["function"]["name"] for tool in BROWSER_TOOLS}
    chat_tools = list(BROWSER_TOOLS)
    if vault_configured(config):
        vault_tool_names = {tool["function"]["name"] for tool in VAULT_TOOLS}
        tool_names |= vault_tool_names
        chat_tools = chat_tools + list(VAULT_TOOLS)

        def vault_executor(method, arguments, sensitive):
            if use_browser_extension:
                return compact_tool_result(
                    server.browser_broker.execute(method, arguments, sensitive)
                )
            return compact_tool_result(native_session.rpc(method, arguments))
    else:
        vault_tool_names = set()
    if ado_configured(config):
        ado_tool_names = {tool["function"]["name"] for tool in ADO_TOOLS}
        tool_names |= ado_tool_names
        chat_tools = chat_tools + list(ADO_TOOLS)
    else:
        ado_tool_names = set()
    launchpad_tool_names = {tool["function"]["name"] for tool in LAUNCHPAD_TOOLS}
    tool_names |= launchpad_tool_names
    chat_tools = chat_tools + list(LAUNCHPAD_TOOLS)
    task_id = str(payload.get("task_id") or uuid.uuid4())
    cancel_event = server.agent_tasks.create(task_id)
    use_browser_extension = should_use_browser_extension(server)
    native_session = (
        None
        if use_browser_extension
        else server.native_agent_hub.create_session(task_id, cancel_event)
    )
    bootstrap_id = None
    task_text = next(
        (
            str(message.get("content", ""))
            for message in reversed(messages)
            if message.get("role") == "user"
        ),
        "",
    )

    def log_progress(event):
        server.agent_tasks.log(task_id, event)

    def cancelled_response():
        if native_session:
            native_session.complete("The autonomous browser task was cancelled by the user.")
        return {
            "message": {
                "role": "assistant",
                "content": "The autonomous browser task was cancelled by the user.",
            },
            "model": model,
            "tool_trace": trace,
            "task_id": task_id,
            "cancelled": True,
        }

    try:
        if use_browser_extension:
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "The cross-browser WebExtension is connected. Use only the exact "
                        "provided browser tools and their JSON schemas. Actions execute in "
                        "the active Chrome, Edge, or Firefox window."
                    ),
                }
            )
        else:
            base_url = (
                f"http://127.0.0.1:{server.server_port}"
                f"?token={quote(server.access_token)}"
            )
            bootstrap_id = server.browser_controller.start_native_agent(
                task_id,
                task_text,
                base_url,
            )
            start_payload = native_session.wait_ready()
            server.browser_controller.close_target(bootstrap_id)
            bootstrap_id = None
            native_context = {
                "url": start_payload.get("url"),
                "tab_id": start_payload.get("tab_id"),
                "tabs_context": start_payload.get("tabs_context"),
                "supported_features": start_payload.get("supported_features"),
                "extension_version": start_payload.get("extension_version"),
            }
            messages.append(
                {
                    "role": "system",
                    "content": (
                        "The signed Comet Agent extension is connected. Use only the exact "
                        "provided Comet RPC tools and their JSON schemas. Current native context: "
                        + json.dumps(native_context)
                    ),
                }
            )
        chat_timeout_seconds = chat_timeout(config)
        round_number = 0
        while True:
            if cancel_event.is_set():
                return cancelled_response()
            messages = compact_agent_messages(messages)
            round_number += 1
            log_progress({"type": "phase", "text": f"Thinking (round {round_number})"})
            # With stream=False the model can stay silent for minutes while
            # loading or planning; retry once so a slow local model does not
            # abort the whole task.
            for attempt in range(2):
                try:
                    _, _, response_body = request_json(
                        target_url(config["endpoint"], "/api/chat"),
                        method="POST",
                        payload={
                            "model": model,
                            "messages": messages,
                            "tools": chat_tools,
                            "stream": False,
                        },
                        api_key=server.api_key,
                        timeout=chat_timeout_seconds,
                    )
                    break
                except TimeoutError:
                    if attempt:
                        raise RuntimeError(
                            "Ollama did not respond within "
                            f"{chat_timeout_seconds} seconds. Raise "
                            "'chat_timeout' in config.json or pick a faster "
                            "model."
                        ) from None
            if cancel_event.is_set():
                return cancelled_response()
            response = json.loads(response_body)
            assistant = response.get("message") or {}
            content_text = str(assistant.get("content") or "")
            thinking_text = (
                assistant.get("thinking") or assistant.get("reasoning") or ""
            )
            inline_match = re.search(
                r"<think(?:ing)?>([\s\S]*?)</think(?:ing)?>",
                content_text,
                re.IGNORECASE,
            )
            if inline_match:
                thinking_text = thinking_text or inline_match.group(1)
            if str(thinking_text).strip():
                log_progress(
                    {"type": "thinking", "text": str(thinking_text).strip()[:600]}
                )
            messages.append(assistant)
            tool_calls = assistant.get("tool_calls") or []
            if not tool_calls:
                if native_session:
                    native_session.complete(assistant.get("content", ""))
                return {
                    "message": assistant,
                    "model": response.get("model", model),
                    "tool_trace": trace,
                    "task_id": task_id,
                }

            for tool_call in tool_calls:
                if cancel_event.is_set():
                    return cancelled_response()
                function = tool_call.get("function") or {}
                name = function.get("name")
                arguments = function.get("arguments") or {}
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                log_progress(
                    {
                        "type": "tool",
                        "tool": name or "unknown",
                        "detail": json.dumps(arguments)[:140],
                    }
                )
                if name not in tool_names:
                    result = {"error": f"Unsupported browser tool: {name}"}
                    raw_result = None
                elif name in vault_tool_names:
                    try:
                        raw_result = load_vault_module().dispatch_tool(
                            name,
                            arguments,
                            config,
                            allow_sensitive,
                            vault_executor,
                        )
                    except (RuntimeError, TimeoutError, ValueError) as error:
                        raw_result = {"error": str(error)}
                    result = compact_tool_result(raw_result)
                elif name in ado_tool_names:
                    try:
                        raw_result = load_ado_module().dispatch_tool(
                            name,
                            arguments,
                            config,
                            allow_sensitive,
                            None,
                        )
                    except (RuntimeError, TimeoutError, ValueError) as error:
                        raw_result = {"error": str(error)}
                    result = compact_tool_result(raw_result)
                elif name in launchpad_tool_names:
                    vault_ready = vault_configured(config)
                    try:
                        raw_result = load_launchpad_module().dispatch_tool(
                            name,
                            arguments,
                            config,
                            allow_sensitive,
                            vault_executor if vault_ready else None,
                            load_vault_module() if vault_ready else None,
                        )
                    except (RuntimeError, TimeoutError, ValueError) as error:
                        raw_result = {"error": str(error)}
                    result = compact_tool_result(raw_result)
                else:
                    try:
                        if not allow_sensitive and name == "FormInput":
                            lowered = json.dumps(arguments).lower()
                            if any(
                                word in lowered
                                for word in (
                                    "password",
                                    "credential",
                                    "card number",
                                    "security code",
                                )
                            ):
                                raise RuntimeError(
                                    "Sensitive input requires explicit user confirmation."
                                )
                        executor = (
                            server.browser_broker.execute
                            if use_browser_extension
                            else native_session.rpc
                        )
                        if use_browser_extension:
                            raw_result = executor(name, arguments, allow_sensitive)
                        else:
                            raw_result = executor(name, arguments)
                    except (RuntimeError, TimeoutError, ValueError) as error:
                        raw_result = {"error": str(error)}
                    result = compact_tool_result(raw_result)
                log_progress(
                    {
                        "type": "tool_result",
                        "tool": name or "unknown",
                        "ok": isinstance(result, dict) and "error" not in result,
                    }
                )
                trace.append({"tool": name, "arguments": arguments, "result": result})
                tool_message = {
                    "role": "tool",
                    "tool_name": name,
                    "content": json.dumps(result),
                }
                images = collect_tool_images(config, model, raw_result)
                if images:
                    tool_message["images"] = images
                messages.append(tool_message)
    finally:
        server.browser_controller.close_target(bootstrap_id)
        if native_session:
            server.native_agent_hub.remove_session(task_id)
        server.agent_tasks.remove(task_id)


def test_lab_page():
    filler = "\n".join(
        f"<p>Scrollable test content row {index}</p>" for index in range(1, 31)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ollama Comet Native Action Lab</title>
  <style>
    body {{ font-family: "Segoe UI", sans-serif; margin: 0; padding: 24px; min-width: 1200px; }}
    main {{ max-width: 900px; }}
    section {{ border: 1px solid #bbb; border-radius: 8px; margin: 16px 0; padding: 16px; }}
    input, textarea, select, button {{ display: block; margin: 8px 0; padding: 8px; }}
    #click-target, #drag-source, #drop-target {{ padding: 24px; border: 2px solid #2563eb; width: 220px; }}
    #drag-source {{ cursor: grab; background: #dbeafe; }}
    #drop-target {{ margin-top: 16px; border-style: dashed; }}
    #wide-area {{ width: 1800px; height: 80px; background: linear-gradient(90deg, #fee2e2, #dcfce7, #dbeafe); }}
    #event-log {{ position: sticky; bottom: 0; background: #111827; color: white; padding: 12px; }}
  </style>
</head>
<body>
<main>
  <h1 id="heading">Ollama Comet Native Action Lab</h1>
  <p id="description">A deterministic page for asserting signed Comet Agent browser actions.</p>
  <section>
    <button id="click-target">Action target</button>
    <label>Text input <input id="text-input" name="text-input" placeholder="Type here"></label>
    <label>Textarea <textarea id="text-area" name="text-area"></textarea></label>
    <label>Selection
      <select id="select-input" name="select-input">
        <option value="alpha">Alpha</option>
        <option value="beta">Beta</option>
        <option value="gamma">Gamma</option>
      </select>
    </label>
    <label><input id="checkbox-input" type="checkbox"> Native checkbox</label>
  </section>
  <section>
    <div id="drag-source" draggable="true">Drag source</div>
    <div id="drop-target">Drop target</div>
  </section>
  <section id="scroll-content">
    <h2>Scroll area</h2>
    {filler}
    <button id="bottom-target">Bottom target</button>
  </section>
  <div id="wide-area">Horizontal scroll test area</div>
</main>
<div id="event-log" aria-live="polite">ready</div>
<script>
const state = {{
  leftClicks: 0,
  rightClicks: 0,
  doubleClicks: 0,
  tripleClicks: 0,
  typed: "",
  selected: "alpha",
  checked: false,
  dropped: false,
  scrollX: 0,
  scrollY: 0
}};
let clickTimes = [];
const log = () => {{
  state.typed = document.getElementById("text-input").value;
  state.selected = document.getElementById("select-input").value;
  state.checked = document.getElementById("checkbox-input").checked;
  state.scrollX = Math.round(scrollX);
  state.scrollY = Math.round(scrollY);
  document.getElementById("event-log").textContent = JSON.stringify(state);
  window.__ollamaCometTestState = {{ ...state }};
}};
const target = document.getElementById("click-target");
target.addEventListener("click", () => {{
  state.leftClicks += 1;
  const now = Date.now();
  clickTimes = [...clickTimes.filter(value => now - value < 800), now];
  if (clickTimes.length >= 3) state.tripleClicks += 1;
  log();
}});
target.addEventListener("dblclick", () => {{ state.doubleClicks += 1; log(); }});
target.addEventListener("contextmenu", event => {{
  event.preventDefault();
  state.rightClicks += 1;
  log();
}});
document.getElementById("text-input").addEventListener("input", log);
document.getElementById("select-input").addEventListener("change", log);
document.getElementById("checkbox-input").addEventListener("change", log);
const dragSource = document.getElementById("drag-source");
const dropTarget = document.getElementById("drop-target");
dragSource.addEventListener("dragstart", event => event.dataTransfer.setData("text/plain", "native"));
dropTarget.addEventListener("dragover", event => event.preventDefault());
dropTarget.addEventListener("drop", event => {{
  event.preventDefault();
  state.dropped = event.dataTransfer.getData("text/plain") === "native";
  log();
}});
addEventListener("scroll", log, {{ passive: true }});
log();
</script>
</body>
</html>"""


def html_page(token):
    safe_token = json.dumps(token)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Ollama for Comet</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: "Segoe UI", system-ui, sans-serif;
      --page: #dff1ee;
      --panel: #f7fbfa;
      --panel-strong: #ffffff;
      --teal: #4f8f89;
      --teal-dark: #356d68;
      --teal-soft: #c9e8e3;
      --border: #abcac5;
      --text: #30383a;
      --muted: #667274;
      --danger: #f9dddd;
    }}
    * {{ box-sizing: border-box; }}
    html, body {{ height: 100%; overflow: hidden; }}
    body {{ margin: 0; background: var(--page); color: var(--text); }}
    main {{
      width: min(100%, 1040px);
      height: 100dvh;
      margin: 0 auto;
      padding: 16px;
      display: grid;
      grid-template-rows: auto auto auto minmax(100px, 1fr) auto;
      gap: 10px;
    }}
    header {{ display: flex; justify-content: space-between; gap: 16px; align-items: center; }}
    h1 {{ margin: 0; font-size: 24px; }}
    .subtle {{ color: var(--muted); font-size: 13px; }}
    .controls {{
      display: grid;
      grid-template-columns: minmax(130px, 0.7fr) minmax(160px, 1.3fr) auto;
      gap: 8px;
    }}
    input, select, button, textarea {{
      border: 1px solid var(--border); border-radius: 10px; background: var(--panel-strong);
      color: inherit; padding: 10px; font: inherit;
    }}
    button {{ cursor: pointer; background: var(--teal); border-color: var(--teal); color: white; font-weight: 600; }}
    button:hover {{ background: var(--teal-dark); }}
    button.secondary {{ background: var(--panel-strong); border-color: var(--border); color: var(--text); }}
    button:disabled {{ cursor: not-allowed; opacity: 0.55; }}
    #messages {{ min-height: 0; overflow-y: auto; display: flex; flex-direction: column; gap: 12px; padding: 4px 2px 12px; }}
    .message {{
      border: 1px solid var(--border); border-radius: 14px; padding: 12px 14px;
      line-height: 1.45; overflow-wrap: anywhere; box-shadow: 0 2px 10px rgba(52, 91, 87, 0.08);
    }}
    .message p {{ margin: 0 0 10px; }}
    .message p:last-child {{ margin-bottom: 0; }}
    .message pre {{ overflow: auto; background: #e8f1ef; padding: 10px; border-radius: 8px; }}
    .message code {{ background: #e8f1ef; padding: 1px 4px; border-radius: 4px; }}
    .message table {{ width: 100%; border-collapse: collapse; margin: 10px 0; font-size: 13px; }}
    .message th, .message td {{ border: 1px solid var(--border); padding: 7px; text-align: left; vertical-align: top; }}
    .message th {{ background: var(--teal-soft); }}
    .message img {{ max-width: min(100%, 520px); max-height: 360px; border-radius: 10px; display: block; margin-top: 10px; }}
    .user {{ background: var(--teal-soft); margin-left: 8%; }}
    .assistant {{ background: var(--panel); margin-right: 4%; }}
    .error {{ background: var(--danger); border-color: #d8a4a4; }}
    textarea {{ width: 100%; min-height: 64px; max-height: 180px; resize: vertical; background: var(--panel-strong); }}
    .composer {{
      display: grid; gap: 8px; padding: 10px; border: 1px solid var(--border);
      border-radius: 14px; background: rgba(247, 251, 250, 0.96);
      box-shadow: 0 -4px 18px rgba(52, 91, 87, 0.1);
    }}
    .attachments {{ display: flex; gap: 8px; overflow-x: auto; }}
    .attachment {{ position: relative; flex: 0 0 auto; }}
    .attachment img {{ width: 88px; height: 66px; object-fit: cover; border-radius: 8px; border: 1px solid var(--border); }}
    .attachment button {{
      position: absolute; top: -6px; right: -6px; width: 22px; height: 22px; padding: 0;
      border-radius: 50%; background: #5b6668; border: 0;
    }}
    .actions {{ display: flex; justify-content: flex-end; align-items: center; gap: 8px; flex-wrap: wrap; }}
    #image-input {{ display: none; }}
    @media (max-width: 720px) {{
      main {{ padding: 10px; }}
      header {{ align-items: flex-start; }}
      h1 {{ font-size: 20px; }}
      .controls {{ grid-template-columns: 1fr 1fr; }}
      .controls button {{ grid-column: 1 / -1; }}
      .user, .assistant {{ margin-left: 0; margin-right: 0; }}
      .actions {{ justify-content: stretch; }}
      .actions button {{ flex: 1 1 auto; }}
    }}
    @media (max-height: 600px) {{
      main {{ padding: 8px; gap: 6px; }}
      header .subtle {{ display: none; }}
      textarea {{ min-height: 48px; max-height: 100px; }}
      .composer {{ padding: 7px; }}
    }}
    #status.busy {{ color: #2563eb; }}
    #status.busy::before {{
      content: "";
      display: inline-block;
      width: 10px; height: 10px;
      margin-right: 6px;
      border: 2px solid #93c5fd;
      border-top-color: #2563eb;
      border-radius: 50%;
      vertical-align: -1px;
      animation: comet-spin 0.8s linear infinite;
    }}
    @keyframes comet-spin {{ to {{ transform: rotate(360deg); }} }}
    .typing {{
      display: flex; align-items: center; gap: 10px;
      margin: 4px 24px 12px 0;
      padding: 10px 14px;
      background: #1f2733; border: 1px solid #374151;
      border-radius: 10px; color: #e5e7eb;
      width: fit-content; max-width: 90%;
    }}
    .typing .dots {{ display: inline-flex; gap: 4px; }}
    .typing .dots span {{
      width: 7px; height: 7px; border-radius: 50%;
      background: #93c5fd; opacity: 0.7;
      animation: comet-bounce 1.2s infinite;
    }}
    .typing .dots span:nth-child(2) {{ animation-delay: 0.15s; }}
    .typing .dots span:nth-child(3) {{ animation-delay: 0.3s; }}
    @keyframes comet-bounce {{
      0%, 60%, 100% {{ transform: translateY(0); opacity: 0.5; }}
      30% {{ transform: translateY(-4px); opacity: 1; }}
    }}
    .activity-label {{ color: #cbd5e1; font-size: 12px; }}
    .activity-feed {{ margin: 6px 0 0 12px; color: #8b98a9; font-size: 11px; }}
    .activity-feed div {{ white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 420px; }}
    .thought-snippet {{ color: #94a3b8; font-style: italic; font-size: 12px; max-width: 520px;
      white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}
    details.thoughts {{
      margin-top: 8px; padding: 6px 10px;
      border-left: 3px solid #4b5563; border-radius: 4px;
      background: rgba(75, 85, 99, 0.18);
      font-size: 12px; color: #cbd5e1; max-width: 640px;
    }}
    details.thoughts summary {{ cursor: pointer; color: #9ca3af; user-select: none; }}
    details.thoughts .thoughts-body {{
      margin-top: 6px; white-space: pre-wrap; word-break: break-word;
      color: #d1d5db; font-size: 12px;
    }}
    .controls.collapsed {{ display: none; }}
    .launchpad {{
      margin-top: 8px; padding: 10px; border: 1px dashed #cdd7d4;
      border-radius: 14px; background: rgba(247, 251, 250, 0.8);
    }}
    .launchpad-head {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; }}
    .launchpad-title {{ font-size: 13px; font-weight: 600; color: #33514c; }}
    .launchpad-buttons {{ display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }}
    .launchpad-buttons button {{ padding: 6px 12px; font-size: 13px; }}
    .launchpad-buttons .empty-note {{ color: #5b6668; font-size: 12px; }}
    .launchpad-editor {{ display: grid; gap: 6px; margin-top: 8px; }}
    .launchpad-editor[hidden] {{ display: none; }}
    .launchpad-editor input, .launchpad-editor select {{
      padding: 6px 8px; border: 1px solid var(--border); border-radius: 8px; font-size: 13px;
    }}
    .launchpad-editor-actions {{ display: flex; gap: 8px; flex-wrap: wrap; }}
    .controls-toggle, .icon-button {{
      width: 36px; height: 36px; padding: 0;
      display: inline-flex; align-items: center; justify-content: center;
    }}
    .controls-toggle svg, .icon-button svg {{ width: 18px; height: 18px; }}
    .controls-toggle[aria-pressed="true"] {{ border-color: #2563eb; color: #93c5fd; }}
    .header-actions {{ display: flex; align-items: center; gap: 8px; flex: 0 0 auto; }}
    .run-stack {{
      display: flex; flex-direction: column; gap: 6px; flex: 0 0 auto;
    }}
    #send {{
      width: 36px; height: 36px; padding: 0;
      display: inline-flex; align-items: center; justify-content: center;
      border-radius: 10px;
    }}
    #send svg {{ width: 20px; height: 20px; }}
    #send.executing {{
      background: #dc2626; border-color: #b91c1c; color: #ffffff;
      animation: stop-pulse 1.2s ease-in-out infinite;
    }}
    #send.executing:disabled {{ opacity: 0.7; cursor: progress; }}
    @keyframes stop-pulse {{
      0%, 100% {{ box-shadow: 0 0 0 0 rgba(220, 38, 38, 0.55); }}
      50% {{ box-shadow: 0 0 0 7px rgba(220, 38, 38, 0); }}
    }}
    .queue-panel {{
      border: 1px solid var(--border); border-radius: 10px;
      background: var(--teal-soft); padding: 6px 8px;
    }}
    .queue-title {{ font-size: 11px; color: var(--teal-dark); margin-bottom: 4px; }}
    #queue-list {{
      list-style: decimal; margin: 0; padding-left: 18px;
      display: flex; flex-direction: column; gap: 4px;
    }}
    #queue-list li {{ display: flex; align-items: center; gap: 6px; font-size: 12px; }}
    .queue-text {{
      flex: 1 1 auto; min-width: 0;
      overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    }}
    #queue-list .queue-now {{ width: 26px; height: 26px; padding: 0; }}
    #queue-list .queue-now svg {{ width: 14px; height: 14px; }}
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Ollama QA Assistant</h1>
      <div class="subtle">Open WebUI-inspired chat with native Comet browser control</div>
    </div>
    <div class="header-actions">
      <button id="settings-toggle" class="secondary controls-toggle" type="button"
        aria-pressed="false" aria-controls="controls"
        aria-label="Show model settings" title="Show model settings">
        <svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">
          <path d="M19.14,12.94c0.04-0.3,0.06-0.61,0.06-0.94c0-0.32-0.02-0.64-0.07-0.94l2.03-1.58c0.18-0.14,0.23-0.41,0.12-0.61 l-1.92-3.32c-0.12-0.22-0.37-0.29-0.59-0.22l-2.39,0.96c-0.5-0.38-1.03-0.7-1.62-0.94L14.4,2.81c-0.04-0.24-0.24-0.41-0.48-0.41 h-3.84c-0.24,0-0.43,0.17-0.47,0.41L9.25,5.35C8.66,5.59,8.12,5.92,7.63,6.29L5.24,5.33c-0.22-0.08-0.47,0-0.59,0.22L2.74,8.87 C2.62,9.08,2.66,9.34,2.86,9.48l2.03,1.58C4.84,11.36,4.8,11.69,4.8,12s0.02,0.64,0.07,0.94l-2.03,1.58 c-0.18,0.14-0.23,0.41-0.12,0.61l1.92,3.32c0.12,0.22,0.37,0.29,0.59,0.22l2.39-0.96c0.5,0.38,1.03,0.7,1.62,0.94l0.36,2.54 c0.05,0.24,0.24,0.41,0.48,0.41h3.84c0.24,0,0.44-0.17,0.47-0.41l0.36-2.54c0.59-0.24,1.13-0.56,1.62-0.94l2.39,0.96 c0.22,0.08,0.47,0,0.59-0.22l1.92-3.32c0.12-0.22,0.07-0.47-0.12-0.61L19.14,12.94z M12,15.6c-1.98,0-3.6-1.62-3.6-3.6 s1.62-3.6,3.6-3.6s3.6,1.62,3.6,3.6S13.98,15.6,12,15.6z"/>
        </svg>
      </button>
      <button id="refresh" class="secondary icon-button" type="button"
        aria-label="Refresh models" title="Refresh models">
        <svg viewBox="0 0 24 24" aria-hidden="true" fill="currentColor">
          <path d="M17.65 6.35C16.2 4.9 14.21 4 12 4c-4.42 0-7.99 3.58-7.99 8s3.57 8 7.99 8c3.73 0 6.84-2.55 7.73-6h-2.08c-.82 2.33-3.04 4-5.65 4-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/>
        </svg>
      </button>
    </div>
  </header>
  <section class="controls" id="controls">
    <select id="provider" aria-label="Model provider">
      <option value="local">Local Ollama</option>
      <option value="cloud">Ollama Cloud</option>
    </select>
    <select id="model" aria-label="Model"></select>
    <button id="save" class="secondary">Save</button>
  </section>
  <section class="launchpad" id="launchpad">
    <div class="launchpad-head">
      <span class="launchpad-title">🚀 Launchpad</span>
      <button id="launchpad-manage" class="secondary" type="button" title="Add, update, or remove launchpad environments">Manage</button>
    </div>
    <div id="launchpad-buttons" class="launchpad-buttons"></div>
    <div id="launchpad-editor" class="launchpad-editor" hidden>
      <input id="lp-name" placeholder="Environment name (e.g. PR1 Production)" aria-label="Environment name">
      <input id="lp-url" placeholder="URL (https://...)" aria-label="Environment URL">
      <input id="lp-account" placeholder="Account email (vault credential)" aria-label="Environment account">
      <div class="launchpad-editor-actions">
        <select id="lp-remove" aria-label="Environment to remove"><option value="">Remove…</option></select>
        <button id="lp-remove-go" class="secondary" type="button">Remove</button>
        <button id="lp-save" class="secondary" type="button">Add / Update</button>
        <button id="lp-done" class="secondary" type="button">Done</button>
      </div>
    </div>
  </section>
  <div id="status" class="subtle"></div>
  <section id="messages"></section>
  <section class="composer">
    <div id="queue-panel" class="queue-panel" hidden>
      <div class="queue-title">Queued - processed next</div>
      <ol id="queue-list"></ol>
    </div>
    <div id="attachments" class="attachments" aria-live="polite"></div>
    <textarea id="prompt" placeholder="Ask Ollama, paste an image or table, attach a document, or give it a browser task..."></textarea>
    <input id="image-input" type="file" accept="image/*,.pdf,.docx,.xlsx" multiple>
    <div class="actions">
      <button id="attach" class="secondary" type="button">Attach files</button>
      <button id="clear" class="secondary">Clear</button>
      <div class="run-stack">
        <button id="send" type="button" title="Send and run task" aria-label="Send and run task">
          <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M8 5v14l11-7z"/>
          </svg>
        </button>
        <button id="queue" class="secondary icon-button" type="button" hidden
          title="Queue message - runs after the current task" aria-label="Queue message">
          <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
            <path d="M14 10H2v2h12v-2zm0-4H2v2h12V6zm4 8v-4h-2v4h-4v2h4v4h2v-4h4v-2h-4z"/>
          </svg>
        </button>
      </div>
    </div>
  </section>
</main>
<script>
const token = {safe_token};
const headers = {{ "Content-Type": "application/json", "X-Ollama-Comet-Token": token }};
const messages = [];
const provider = document.getElementById("provider");
const model = document.getElementById("model");
const status = document.getElementById("status");
const messagesEl = document.getElementById("messages");
const promptEl = document.getElementById("prompt");
const sendButton = document.getElementById("send");
const queueButton = document.getElementById("queue");
const PLAY_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5v14l11-7z"/></svg>';
const STOP_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M6 6h12v12H6z"/></svg>';
const FAST_FORWARD_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M4 18l8.5-6L4 6v12zm9-12v12l8.5-6L13 6z"/></svg>';
const RESUME_PREFIX = "[This task was interrupted for a more urgent request. Review the progress above and resume this task only if it remains appropriate; otherwise say so briefly.]\n";
let taskRunning = false;
let stopRequested = false;
let taskQueue = [];
let runningTask = null;
const attachmentsEl = document.getElementById("attachments");
const imageInput = document.getElementById("image-input");
const pendingAttachments = [];
let currentTaskId = null;
let progressTimer = null;
let progressIndex = 0;
const activityLines = [];
let latestThinking = "";
let typingEl = null;
let currentLabel = "";

function splitThinking(content) {{
  const text = String(content || "");
  const match = text.match(/<think(?:ing)?>([\\s\\S]*?)<\\/think(?:ing)?>/i);
  if (!match) return {{ thinking: "", body: text }};
  return {{
    thinking: match[1].trim(),
    body: (text.slice(0, match.index) + text.slice(match.index + match[0].length)).trim()
  }};
}}

function appendThoughts(element, thinking) {{
  const text = String(thinking || "").trim();
  if (!text) return;
  const details = document.createElement("details");
  details.className = "thoughts";
  const summary = document.createElement("summary");
  summary.textContent = "Thought process";
  const body = document.createElement("div");
  body.className = "thoughts-body";
  body.textContent = text;
  details.appendChild(summary);
  details.appendChild(body);
  element.insertBefore(details, element.firstChild);
}}

function showTyping(label) {{
  hideTyping();
  typingEl = document.createElement("div");
  typingEl.className = "typing";
  const dots = document.createElement("span");
  dots.className = "dots";
  for (let i = 0; i < 3; i += 1) dots.appendChild(document.createElement("span"));
  typingEl.appendChild(dots);
  const labelEl = document.createElement("span");
  labelEl.className = "activity-label";
  labelEl.textContent = label || "Working...";
  typingEl.appendChild(labelEl);
  const snippet = document.createElement("span");
  snippet.className = "thought-snippet";
  snippet.textContent = "";
  typingEl.appendChild(snippet);
  const feed = document.createElement("div");
  feed.className = "activity-feed";
  typingEl.appendChild(feed);
  messagesEl.appendChild(typingEl);
  typingEl.scrollIntoView({{ behavior: "smooth", block: "end" }});
}}

function updateTypingFeed() {{
  if (!typingEl) return;
  const label = typingEl.querySelector(".activity-label");
  const snippet = typingEl.querySelector(".thought-snippet");
  const feed = typingEl.querySelector(".activity-feed");
  if (label && currentLabel) label.textContent = currentLabel;
  if (snippet) {{
    snippet.textContent = latestThinking ? latestThinking.split(/\\n/)[0] : "";
    snippet.title = latestThinking || "";
  }}
  if (feed) {{
    feed.replaceChildren();
    for (const line of activityLines.slice(-4)) {{
      const row = document.createElement("div");
      row.textContent = line;
      feed.appendChild(row);
    }}
  }}
}}

function hideTyping() {{
  if (typingEl) typingEl.remove();
  typingEl = null;
}}

function startProgressPolling(taskId) {{
  stopProgressPolling();
  progressIndex = 0;
  progressTimer = setInterval(async () => {{
    if (!currentTaskId) return;
    try {{
      const response = await fetch(
        `/api/agent/progress?task_id=${{encodeURIComponent(taskId)}}&after=${{progressIndex}}`,
        {{ headers }}
      );
      if (!response.ok) return;
      const data = await response.json();
      if (!data.active) {{
        stopProgressPolling();
        return;
      }}
      progressIndex = data.index || progressIndex;
      for (const event of data.events || []) {{
        if (event.type === "phase") currentLabel = event.text;
        else if (event.type === "thinking") {{
          latestThinking = event.text;
          activityLines.push(`💭 ${{String(event.text).split(/\\n/)[0].slice(0, 90)}}`);
        }}
        else if (event.type === "tool") {{
          activityLines.push(`🔧 ${{event.tool}} ${{event.detail || ""}}`);
          currentLabel = `Running tool: ${{event.tool}}`;
        }}
        else if (event.type === "tool_result") {{
          activityLines.push(`${{event.ok ? "✓" : "✗"}} ${{event.tool}} finished`);
        }}
      }}
      if (currentLabel && typingEl) typingEl.querySelector(".activity-label").textContent = currentLabel;
      updateTypingFeed();
    }} catch (error) {{
      /* Polling is best-effort only; never disturb the chat flow. */
    }}
  }}, 1000);
}}

function stopProgressPolling() {{
  if (progressTimer) clearInterval(progressTimer);
  progressTimer = null;
}}

function escapeHtml(value) {{
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}}

function inlineMarkdown(value) {{
  return escapeHtml(value)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\\*\\*([^*]+)\\*\\*/g, "<strong>$1</strong>")
    .replace(/\\*([^*]+)\\*/g, "<em>$1</em>");
}}

function renderMarkdown(content) {{
  const lines = String(content || "").replaceAll("\\r\\n", "\\n").split("\\n");
  const output = [];
  let index = 0;
  let inCode = false;
  let codeLines = [];
  while (index < lines.length) {{
    const line = lines[index];
    if (line.trim().startsWith("```")) {{
      if (inCode) {{
        output.push(`<pre><code>${{escapeHtml(codeLines.join("\\n"))}}</code></pre>`);
        codeLines = [];
      }}
      inCode = !inCode;
      index += 1;
      continue;
    }}
    if (inCode) {{
      codeLines.push(line);
      index += 1;
      continue;
    }}
    const next = lines[index + 1] || "";
    if (
      line.includes("|") &&
      /^\\s*\\|?\\s*:?-{{3,}}/.test(next)
    ) {{
      const headersRow = line.replace(/^\\||\\|$/g, "").split("|").map(cell => cell.trim());
      index += 2;
      const rows = [];
      while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {{
        rows.push(lines[index].replace(/^\\||\\|$/g, "").split("|").map(cell => cell.trim()));
        index += 1;
      }}
      output.push("<table><thead><tr>" + headersRow.map(cell => `<th>${{inlineMarkdown(cell)}}</th>`).join("") +
        "</tr></thead><tbody>" + rows.map(row => "<tr>" +
          headersRow.map((_, cellIndex) => `<td>${{inlineMarkdown(row[cellIndex] || "")}}</td>`).join("") +
          "</tr>").join("") + "</tbody></table>");
      continue;
    }}
    if (/^###\\s+/.test(line)) output.push(`<h3>${{inlineMarkdown(line.replace(/^###\\s+/, ""))}}</h3>`);
    else if (/^##\\s+/.test(line)) output.push(`<h2>${{inlineMarkdown(line.replace(/^##\\s+/, ""))}}</h2>`);
    else if (/^#\\s+/.test(line)) output.push(`<h1>${{inlineMarkdown(line.replace(/^#\\s+/, ""))}}</h1>`);
    else if (/^[-*]\\s+/.test(line)) {{
      const items = [];
      while (index < lines.length && /^[-*]\\s+/.test(lines[index])) {{
        items.push(`<li>${{inlineMarkdown(lines[index].replace(/^[-*]\\s+/, ""))}}</li>`);
        index += 1;
      }}
      output.push(`<ul>${{items.join("")}}</ul>`);
      continue;
    }} else if (line.trim()) output.push(`<p>${{inlineMarkdown(line)}}</p>`);
    else output.push("<br>");
    index += 1;
  }}
  if (codeLines.length) output.push(`<pre><code>${{escapeHtml(codeLines.join("\\n"))}}</code></pre>`);
  return output.join("");
}}

function addMessage(role, content, isError = false, imageUrls = [], thinking = "") {{
  const element = document.createElement("div");
  element.className = `message ${{isError ? "error" : role}}`;
  element.innerHTML = renderMarkdown(content);
  for (const imageUrl of imageUrls) {{
    const image = document.createElement("img");
    image.src = imageUrl;
    image.alt = "Attached image";
    element.appendChild(image);
  }}
  appendThoughts(element, thinking);
  messagesEl.appendChild(element);
  element.scrollIntoView({{ behavior: "smooth", block: "end" }});
}}

function renderAttachments() {{
  attachmentsEl.replaceChildren();
  pendingAttachments.forEach((attachment, index) => {{
    const wrapper = document.createElement("div");
    wrapper.className = "attachment";
    if (attachment.type.startsWith("image/")) {{
      const image = document.createElement("img");
      image.src = attachment.preview;
      image.alt = attachment.name;
      wrapper.appendChild(image);
    }} else {{
      const card = document.createElement("div");
      card.className = "message";
      card.textContent = attachment.name;
      wrapper.appendChild(card);
    }}
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "x";
    remove.title = `Remove ${{attachment.name}}`;
    remove.addEventListener("click", () => {{
      pendingAttachments.splice(index, 1);
      renderAttachments();
    }});
    wrapper.appendChild(remove);
    attachmentsEl.appendChild(wrapper);
  }});
}}

function fileToAttachment(file) {{
  return new Promise((resolve, reject) => {{
    if (file.size > 20 * 1024 * 1024) {{
      reject(new Error(`${{file.name}} exceeds the 20 MB attachment limit.`));
      return;
    }}
    const supported = file.type.startsWith("image/") ||
      /\\.(pdf|docx|xlsx)$/i.test(file.name);
    if (!supported) {{
      reject(new Error(`${{file.name}} is not supported. Use images, PDF, DOCX, or XLSX.`));
      return;
    }}
    const reader = new FileReader();
    reader.onerror = () => reject(new Error(`Unable to read ${{file.name}}.`));
    reader.onload = () => {{
      const preview = String(reader.result);
      resolve({{
        name: file.name,
        type: file.type || "application/octet-stream",
        data: preview.split(",", 2)[1],
        preview
      }});
    }};
    reader.readAsDataURL(file);
  }});
}}

async function addFiles(files) {{
  try {{
    for (const file of [...files].slice(0, 6 - pendingAttachments.length)) {{
      pendingAttachments.push(await fileToAttachment(file));
    }}
    renderAttachments();
    status.textContent = `${{pendingAttachments.length}} attachment(s) ready`;
  }} catch (error) {{
    status.textContent = error.message;
  }}
}}

async function loadConfig() {{
  const response = await fetch("/api/config", {{ headers }});
  const config = await response.json();
  provider.value = config.mode;
  await loadModels(config[`${{config.mode}}_model`] || config.model);
  status.textContent = config.mode === "cloud" && config.cloud_configured
    ? "Ollama Cloud: API key is loaded from Windows-protected storage."
    : config.mode === "cloud"
      ? "Ollama Cloud needs setup. Run: ollama launch comet --config"
    : "Local mode: requests stay on this computer.";
  await loadLaunchpad();
}}

async function loadLaunchpad() {{
  const container = document.getElementById("launchpad-buttons");
  if (!container) return;
  try {{
    const response = await fetch("/api/config", {{ headers }});
    const config = await response.json();
    renderLaunchpad(config.environments || {{}});
  }} catch (error) {{
    container.replaceChildren();
  }}
}}

function renderLaunchpad(environments) {{
  const container = document.getElementById("launchpad-buttons");
  const removeSelect = document.getElementById("lp-remove");
  const entries = Object.entries(environments || {{}});
  container.replaceChildren();
  if (removeSelect) {{
    while (removeSelect.options.length > 1) removeSelect.remove(1);
    for (const [name] of entries) {{
      const option = document.createElement("option");
      option.value = name;
      option.textContent = name;
      removeSelect.appendChild(option);
    }}
  }}
  if (!entries.length) {{
    const note = document.createElement("span");
    note.className = "empty-note";
    note.textContent = "No environments configured yet — click Manage to add one.";
    container.appendChild(note);
    return;
  }}
  for (const [name, value] of entries) {{
    const button = document.createElement("button");
    button.type = "button";
    button.className = "launch-env";
    button.textContent = name;
    button.title = `Open ${{value.url}} and sign in as ${{value.account}} with its vault credential`;
    button.addEventListener("click", () => launchEnv(name, value));
    container.appendChild(button);
  }}
}}

async function launchEnv(name, value) {{
  promptEl.value = [
    `Launch the environment "${{name}}" for account ${{value.account}}:`,
    `open ${{value.url}}, fill the login form with its vault credential and submit.`,
    "I confirm the sign in for this launch."
  ].join(" ");
  await sendMessage();
}}

async function loadModels(preferred) {{
  const selectedProvider = provider.value;
  status.textContent = `Loading ${{selectedProvider}} models...`;
  try {{
    const response = await fetch(`/api/models?mode=${{encodeURIComponent(selectedProvider)}}`, {{ headers }});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to list models");
    model.replaceChildren();
    for (const item of data.models || []) {{
      const option = document.createElement("option");
      option.value = item.name || item.model;
      option.textContent = item.name || item.model;
      model.appendChild(option);
    }}
    if (preferred && ![...model.options].some(option => option.value === preferred)) {{
      const option = document.createElement("option");
      option.value = preferred;
      option.textContent = preferred;
      model.appendChild(option);
    }}
    model.value = preferred || model.options[0]?.value || "";
    status.textContent = `${{model.options.length}} ${{selectedProvider}} model(s) available`;
  }} catch (error) {{
    model.replaceChildren();
    status.textContent = error.message;
  }}
}}

async function saveConfig() {{
  const response = await fetch("/api/config", {{
    method: "POST",
    headers,
    body: JSON.stringify({{ mode: provider.value, model: model.value }})
  }});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Unable to save configuration");
  status.textContent = "Configuration saved.";
}}

function updateSendButton() {{
  if (taskRunning) {{
    sendButton.innerHTML = STOP_SVG;
    sendButton.classList.add("executing");
    const label = stopRequested ? "Stopping task..." : "Stop autonomous task";
    sendButton.title = label;
    sendButton.setAttribute("aria-label", label);
    sendButton.disabled = stopRequested;
  }} else {{
    sendButton.innerHTML = PLAY_SVG;
    sendButton.classList.remove("executing");
    sendButton.title = "Send and run task";
    sendButton.setAttribute("aria-label", "Send and run task");
    sendButton.disabled = false;
  }}
  updateQueueButton();
}}

function updateQueueButton() {{
  const visible = taskRunning && !stopRequested;
  queueButton.hidden = !visible;
  queueButton.disabled = !visible || (!promptEl.value.trim() && !pendingAttachments.length);
}}

function startTask(content, attachments, imageUrls) {{
  taskRunning = true;
  runningTask = {{ content }};
  updateSendButton();
  return runTask(content, attachments, imageUrls);
}}

function advanceQueue() {{
  if (!taskQueue.length) {{
    updateSendButton();
    return;
  }}
  const next = taskQueue.shift();
  renderQueue();
  status.textContent = "Starting next queued message...";
  startTask(next.content, next.attachments, next.imageUrls);
}}

async function runTask(content, attachments, imageUrls) {{
  messages.push({{ role: "user", content, attachments }});
  addMessage("user", content || "Attached files", false, imageUrls);
  status.textContent = "Thinking and controlling Comet...";
  status.classList.add("busy");
  currentTaskId = crypto.randomUUID();
  activityLines.length = 0;
  latestThinking = "";
  currentLabel = "Thinking...";
  showTyping(currentLabel);
  startProgressPolling(currentTaskId);
  try {{
    await saveConfig();
    const response = await fetch("/api/agent", {{
      method: "POST",
      headers,
      body: JSON.stringify({{
        task_id: currentTaskId,
        model: model.value,
        messages,
        stream: false
      }})
    }});
    const data = await response.json();
    window.__ollamaCometLastResult = data;
    if (!response.ok) throw new Error(data.error || "Ollama request failed");
    const answerRaw = data.message?.content || data.response || JSON.stringify(data, null, 2);
    const split = splitThinking(answerRaw);
    const modelThinking = data.message?.thinking || data.message?.reasoning || "";
    messages.push({{ role: "assistant", content: answerRaw }});
    addMessage("assistant", split.body, false, [], split.thinking || modelThinking);
    const used = (data.tool_trace || []).map(item => item.tool);
    status.textContent = used.length
      ? `Ready - browser tools used: ${{used.join(", ")}}`
      : "Ready";
  }} catch (error) {{
    window.__ollamaCometLastResult = {{ error: error.message }};
    addMessage("assistant", error.message, true);
    status.textContent = "Request failed";
  }} finally {{
    stopProgressPolling();
    hideTyping();
    status.classList.remove("busy");
    taskRunning = false;
    stopRequested = false;
    runningTask = null;
    currentTaskId = null;
    if (taskQueue.length) {{
      advanceQueue();
    }} else {{
      updateSendButton();
    }}
  }}
}}

async function sendMessage() {{
  if (taskRunning) {{
    requestStop();
    return;
  }}
  const content = promptEl.value.trim();
  if (!content && !pendingAttachments.length) return;
  const attachments = pendingAttachments.map(attachment => ({{
    name: attachment.name,
    type: attachment.type,
    data: attachment.data
  }}));
  const imageUrls = pendingAttachments
    .filter(attachment => attachment.type.startsWith("image/"))
    .map(attachment => attachment.preview);
  promptEl.value = "";
  pendingAttachments.length = 0;
  renderAttachments();
  startTask(content, attachments, imageUrls);
}}

async function requestStop() {{
  if (!currentTaskId || stopRequested) return;
  stopRequested = true;
  updateSendButton();
  status.textContent = "Stopping autonomous task...";
  try {{
    const response = await fetch("/api/agent/cancel", {{
      method: "POST",
      headers,
      body: JSON.stringify({{ task_id: currentTaskId }})
    }});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to stop task");
    status.textContent = data.cancelled
      ? "Stopping autonomous task..."
      : "Task already finished.";
  }} catch (error) {{
    status.textContent = error.message;
    stopRequested = false;
    updateSendButton();
  }}
}}

function queueMessage() {{
  if (!taskRunning || stopRequested) return;
  const content = promptEl.value.trim();
  if (!content && !pendingAttachments.length) return;
  const attachments = pendingAttachments.map(attachment => ({{
    name: attachment.name,
    type: attachment.type,
    data: attachment.data
  }}));
  const imageUrls = pendingAttachments
    .filter(attachment => attachment.type.startsWith("image/"))
    .map(attachment => attachment.preview);
  promptEl.value = "";
  pendingAttachments.length = 0;
  renderAttachments();
  taskQueue.push({{ content, attachments, imageUrls, resume: false }});
  renderQueue();
  updateQueueButton();
  status.textContent = `Message queued - ${{taskQueue.length}} waiting.`;
}}

function renderQueue() {{
  const panel = document.getElementById("queue-panel");
  const list = document.getElementById("queue-list");
  if (!panel || !list) return;
  list.replaceChildren();
  panel.hidden = taskQueue.length === 0;
  taskQueue.forEach((item, index) => {{
    const row = document.createElement("li");
    const text = document.createElement("span");
    text.className = "queue-text";
    text.textContent = item.content || "Attached files";
    text.title = item.content || "Attached files";
    const now = document.createElement("button");
    now.className = "secondary icon-button queue-now";
    now.type = "button";
    now.title = "Send now - interrupts the running task";
    now.setAttribute("aria-label", "Send this queued message now");
    now.innerHTML = FAST_FORWARD_SVG;
    now.addEventListener("click", () => sendQueuedNow(index));
    row.append(text, now);
    list.appendChild(row);
  }});
}}

function sendQueuedNow(index) {{
  if (index >= taskQueue.length) return;
  const urgent = taskQueue.splice(index, 1)[0];
  if (!taskRunning) {{
    renderQueue();
    startTask(urgent.content, urgent.attachments, urgent.imageUrls);
    return;
  }}
  const queue = [urgent];
  if (runningTask && runningTask.content) {{
    queue.push({{
      content: RESUME_PREFIX + runningTask.content,
      attachments: [],
      imageUrls: [],
      resume: true
    }});
  }}
  taskQueue = queue.concat(taskQueue);
  renderQueue();
  updateQueueButton();
  requestStop();
}}

function setControlsCollapsed(collapsed) {{
  const controls = document.getElementById("controls");
  const toggle = document.getElementById("settings-toggle");
  if (!controls || !toggle) return;
  controls.classList.toggle("collapsed", collapsed);
  toggle.setAttribute("aria-pressed", collapsed ? "true" : "false");
  const label = collapsed ? "Show model settings" : "Hide model settings";
  toggle.title = label;
  toggle.setAttribute("aria-label", label);
  try {{
    localStorage.setItem("ollamaCometControlsCollapsed", collapsed ? "1" : "0");
  }} catch (error) {{
    /* localStorage may be unavailable; collapse still works for this view. */
  }}
}}

async function saveAndCollapse() {{
  try {{
    await saveConfig();
    setControlsCollapsed(true);
    status.textContent = "Configuration saved - settings collapsed (⚙ to reopen)";
  }} catch (error) {{
    status.textContent = error.message;
  }}
}}

const launchpadEditor = document.getElementById("launchpad-editor");
document.getElementById("launchpad-manage").addEventListener("click", () => {{
  launchpadEditor.hidden = !launchpadEditor.hidden;
}});
document.getElementById("lp-done").addEventListener("click", () => {{
  launchpadEditor.hidden = true;
  document.getElementById("lp-name").value = "";
  document.getElementById("lp-url").value = "";
  document.getElementById("lp-account").value = "";
}});
document.getElementById("lp-save").addEventListener("click", async () => {{
  try {{
    const name = document.getElementById("lp-name").value.trim();
    const url = document.getElementById("lp-url").value.trim();
    const account = document.getElementById("lp-account").value.trim();
    if (!name) throw new Error("Environment name is required.");
    if (!url || !account) throw new Error("Both URL and account are required.");
    const response = await fetch("/api/config", {{ headers }});
    const config = await response.json();
    const environments = config.environments || {{}};
    environments[name] = {{ url, account }};
    const save = await fetch("/api/config", {{
      method: "POST",
      headers,
      body: JSON.stringify({{ mode: provider.value, model: model.value, environments }})
    }});
    const data = await save.json();
    if (!save.ok) throw new Error(data.error || "Unable to save environment");
    document.getElementById("lp-name").value = "";
    document.getElementById("lp-url").value = "";
    document.getElementById("lp-account").value = "";
    renderLaunchpad(environments);
    status.textContent = `Environment "${{name}}" saved.`;
  }} catch (error) {{
    status.textContent = error.message;
  }}
}});
document.getElementById("lp-remove-go").addEventListener("click", async () => {{
  const removeName = document.getElementById("lp-remove").value;
  if (!removeName) return;
  try {{
    const response = await fetch("/api/config", {{ headers }});
    const config = await response.json();
    const environments = config.environments || {{}};
    delete environments[removeName];
    const save = await fetch("/api/config", {{
      method: "POST",
      headers,
      body: JSON.stringify({{ mode: provider.value, model: model.value, environments }})
    }});
    const data = await save.json();
    if (!save.ok) throw new Error(data.error || "Unable to remove environment");
    renderLaunchpad(environments);
    status.textContent = `Environment "${{removeName}}" removed.`;
  }} catch (error) {{
    status.textContent = error.message;
  }}
}});
document.getElementById("send").addEventListener("click", sendMessage);
document.getElementById("save").addEventListener("click", saveAndCollapse);
document.getElementById("settings-toggle").addEventListener("click", () => {{
  setControlsCollapsed(document.getElementById("controls").classList.contains("collapsed") ? false : true);
}});
try {{
  if (localStorage.getItem("ollamaCometControlsCollapsed") !== "0") setControlsCollapsed(true);
}} catch (error) {{
  /* localStorage unavailable - keep settings collapsed for this view. */
}}
document.getElementById("refresh").addEventListener("click", () => loadModels(model.value));
document.getElementById("attach").addEventListener("click", () => imageInput.click());
imageInput.addEventListener("change", async () => {{
  await addFiles(imageInput.files);
  imageInput.value = "";
}});
document.getElementById("queue").addEventListener("click", queueMessage);
promptEl.addEventListener("input", updateQueueButton);
updateSendButton();
provider.addEventListener("change", async () => {{
  const response = await fetch("/api/config", {{ headers }});
  const config = await response.json();
  await loadModels(config[`${{provider.value}}_model`] || "");
}});
document.getElementById("clear").addEventListener("click", () => {{
  messages.length = 0;
  pendingAttachments.length = 0;
  renderAttachments();
  messagesEl.replaceChildren();
}});
promptEl.addEventListener("paste", async event => {{
  const files = [...(event.clipboardData?.files || [])];
  if (files.length) {{
    event.preventDefault();
    await addFiles(files);
  }}
}});
promptEl.addEventListener("keydown", event => {{
  if (event.key === "Enter" && !event.shiftKey) {{
    event.preventDefault();
    sendMessage();
  }}
}});
loadConfig();
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "OllamaCometBridge/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        message = (fmt % args).encode("ascii", errors="backslashreplace").decode("ascii")
        sys.stdout.write(f"{self.address_string()} - {message}\n")
        sys.stdout.flush()

    def send_bytes(self, status, content_type, body, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "connect-src 'self'; img-src 'self' data: blob:",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def send_json(self, status, value):
        self.send_bytes(status, "application/json; charset=utf-8", json.dumps(value).encode("utf-8"))

    def authorized(self):
        parsed = urlparse(self.path)
        query_token = parse_qs(parsed.query).get("token", [None])[0]
        header_token = self.headers.get("X-Ollama-Comet-Token")
        cookies = SimpleCookie(self.headers.get("Cookie", ""))
        cookie_token = cookies.get("ollama_comet_token")
        return secrets.compare_digest(
            query_token or header_token or (cookie_token.value if cookie_token else ""),
            self.server.access_token,
        )

    def read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length) or b"{}")

    def handle_native_websocket(self):
        if not self.authorized():
            self.send_json(403, {"error": "Invalid native agent token"})
            return
        websocket_key = self.headers.get("Sec-WebSocket-Key", "")
        if not websocket_key:
            self.send_json(400, {"error": "Missing WebSocket key"})
            return
        accept = base64.b64encode(
            hashlib.sha1(
                (websocket_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode(
                    "ascii"
                )
            ).digest()
        ).decode("ascii")
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", accept)
        self.end_headers()

        connection = NativeWebSocketConnection(self)
        fragmented_opcode = None
        fragmented_payload = bytearray()
        try:
            while True:
                finished, opcode, payload = connection.read_frame()
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    connection.send_frame(0xA, payload)
                    continue
                if opcode == 0x1:
                    if finished:
                        message_payload = payload
                    else:
                        fragmented_opcode = opcode
                        fragmented_payload = bytearray(payload)
                        continue
                elif opcode == 0x0 and fragmented_opcode == 0x1:
                    fragmented_payload.extend(payload)
                    if not finished:
                        continue
                    message_payload = bytes(fragmented_payload)
                    fragmented_opcode = None
                    fragmented_payload.clear()
                else:
                    continue
                self.server.native_agent_hub.handle_message(
                    connection,
                    json.loads(message_payload.decode("utf-8")),
                )
        except (EOFError, OSError, ValueError, json.JSONDecodeError):
            pass
        finally:
            connection.closed = True
            self.server.native_agent_hub.disconnect(connection)
            self.close_connection = True

    def do_GET(self):
        parsed = urlparse(self.path)
        if (
            parsed.path == "/agent"
            and self.headers.get("Upgrade", "").lower() == "websocket"
        ):
            self.close_connection = True
            self.handle_native_websocket()
            return
        if parsed.path == "/health":
            self.send_json(
                200,
                {
                    "status": "ok",
                    "browser_target": self.server.browser_target,
                    "extension_control": (
                        "connected" if self.server.browser_broker.connected() else "waiting"
                    ),
                    "native_comet_control": (
                        "connected" if self.server.browser_controller.connected() else "waiting"
                    ),
                },
            )
            return

        if parsed.path == "/api/browser/next":
            if not self.authorized():
                self.send_json(403, {"error": "Invalid browser-control token"})
                return
            command = self.server.browser_broker.next_command()
            if command is None:
                self.send_bytes(204, "application/json", b"")
            else:
                self.send_json(200, command)
            return

        if parsed.path == "/api/agent/progress":
            if not self.authorized():
                self.send_json(403, {"error": "Invalid launcher token"})
                return
            query = parse_qs(parsed.query)
            task_id = (query.get("task_id") or [""])[0]
            if not task_id:
                self.send_json(400, {"error": "task_id is required"})
                return
            try:
                after = max(0, int((query.get("after") or ["0"])[0]))
            except ValueError:
                after = 0
            self.send_json(200, self.server.agent_tasks.progress(task_id, after))
            return

        if parsed.path in {"/", "/sidecar", "/sidecar/", "/sidecar/search/new", "/embedded-sidecar/search/new"}:
            body = html_page(self.server.access_token).encode("utf-8")
            self.send_bytes(
                200,
                "text/html; charset=utf-8",
                body,
                {
                    "Set-Cookie": (
                        f"ollama_comet_token={self.server.access_token}; "
                        "Path=/; HttpOnly; SameSite=Strict"
                    )
                },
            )
            return

        if parsed.path == "/test-lab":
            body = test_lab_page().encode("utf-8")
            self.send_bytes(200, "text/html; charset=utf-8", body)
            return

        if parsed.path == "/api/config":
            if not self.authorized():
                self.send_json(403, {"error": "Invalid launcher token"})
                return
            config = load_config()
            config["cloud_configured"] = bool(self.server.api_key)
            self.send_json(200, config)
            return

        if parsed.path == "/api/models":
            if not self.authorized():
                self.send_json(403, {"error": "Invalid launcher token"})
                return
            config = load_config()
            requested_mode = parse_qs(parsed.query).get("mode", [config["mode"]])[0]
            if requested_mode not in {"local", "cloud"}:
                self.send_json(400, {"error": "Mode must be local or cloud"})
                return
            if requested_mode == "cloud" and not self.server.api_key:
                self.send_json(
                    412,
                    {
                        "error": (
                            "Ollama Cloud is not configured. Run "
                            "'ollama launch comet --config' and securely enter an API key."
                        )
                    },
                )
                return
            try:
                status, content_type, body = request_json(
                    target_url(config[f"{requested_mode}_endpoint"], "/api/tags"),
                    api_key=self.server.api_key if requested_mode == "cloud" else None,
                    timeout=30,
                )
                self.send_bytes(status, content_type, body)
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                self.send_json(error.code, {"error": detail or str(error)})
            except (urllib.error.URLError, TimeoutError) as error:
                self.send_json(502, {"error": f"Unable to reach Ollama: {error}"})
            return

        if parsed.path == "/api/user":
            self.send_json(200, {})
            return
        if parsed.path == "/rest/user/settings":
            self.send_json(200, {})
            return
        if parsed.path == "/rest/enterprise/user/organization":
            self.send_json(200, {})
            return
        if parsed.path == "/rest/browser/update":
            self.send_json(200, {"body": {}})
            return
        if parsed.path == "/asi-comet-relay/sse/remote-control":
            self.send_json(501, {"error": "Perplexity remote-control protocol is not implemented"})
            return

        self.send_json(404, {"error": f"Unsupported compatibility endpoint: {parsed.path}"})

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path in {"/rest/event/analytics", "/rest/metrics/collect"}:
            self.send_json(204, {})
            return
        if parsed.path == "/rest/browser/partners":
            self.send_json(200, {})
            return
        if parsed.path == "/rest/autosuggest/list-autosuggest":
            self.send_json(200, {"suggestions": []})
            return

        if not self.authorized():
            self.send_json(403, {"error": "Invalid launcher token"})
            return

        if parsed.path == "/api/browser/result":
            try:
                body = self.read_json()
                command_id = str(body.get("id", ""))
                if not command_id:
                    raise ValueError("Browser result id is required")
                self.server.browser_broker.complete(command_id, body.get("result") or {})
                self.send_json(200, {"accepted": True})
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json(400, {"error": str(error)})
            return

        if parsed.path == "/api/agent/cancel":
            try:
                body = self.read_json()
                task_id = str(body.get("task_id", ""))
                if not task_id:
                    raise ValueError("task_id is required")
                cancelled = self.server.agent_tasks.cancel(task_id)
                self.send_json(200, {"task_id": task_id, "cancelled": cancelled})
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json(400, {"error": str(error)})
            return

        if parsed.path == "/api/config":
            try:
                body = self.read_json()
                config = load_config()
                mode = str(body.get("mode", config["mode"])).strip().lower()
                model = str(body.get("model", config["model"])).strip()
                if mode not in {"local", "cloud"}:
                    raise ValueError("Mode must be local or cloud")
                if mode == "cloud" and not self.server.api_key:
                    self.send_json(
                        412,
                        {
                            "error": (
                                "Ollama Cloud is not configured. Run "
                                "'ollama launch comet --config' first."
                            )
                        },
                    )
                    return
                if not model:
                    raise ValueError("Model is required")
                config["mode"] = mode
                config[f"{mode}_model"] = model
                config["endpoint"] = config[f"{mode}_endpoint"]
                config["model"] = model
                if "environments" in body:
                    raw_envs = body.get("environments") or {}
                    if not isinstance(raw_envs, dict):
                        raise ValueError("Environments must be an object")
                    clean_envs = {}
                    for env_name, env_value in raw_envs.items():
                        label = str(env_name).strip()
                        if not label or not isinstance(env_value, dict):
                            continue
                        url = str(env_value.get("url") or "").strip()
                        account = str(env_value.get("account") or "").strip()
                        if not url or not account:
                            raise ValueError(
                                f"Environment '{label}' needs both url and account."
                            )
                        clean_envs[label] = {"url": url, "account": account}
                    config["environments"] = clean_envs
                save_config(config)
                config["cloud_configured"] = bool(self.server.api_key)
                self.send_json(200, config)
            except (ValueError, json.JSONDecodeError) as error:
                self.send_json(400, {"error": str(error)})
            return

        if parsed.path == "/api/chat":
            try:
                payload = self.read_json()
                config = load_config()
                payload["model"] = payload.get("model") or config["model"]
                payload["stream"] = False
                status, content_type, body = request_json(
                    target_url(config["endpoint"], "/api/chat"),
                    method="POST",
                    payload=payload,
                    api_key=self.server.api_key,
                    timeout=chat_timeout(config),
                )
                self.send_bytes(status, content_type, body)
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                self.send_json(error.code, {"error": detail or str(error)})
            except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as error:
                self.send_json(502, {"error": f"Ollama request failed: {error}"})
            return

        if parsed.path == "/api/agent":
            try:
                payload = self.read_json()
                self.send_json(200, run_agent(self.server, payload))
            except urllib.error.HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                self.send_json(error.code, {"error": detail or str(error)})
            except (
                urllib.error.URLError,
                TimeoutError,
                RuntimeError,
                ValueError,
                json.JSONDecodeError,
            ) as error:
                self.send_json(502, {"error": f"Ollama browser agent failed: {error}"})
            return

        self.send_json(501, {"error": f"Perplexity endpoint is not implemented: {parsed.path}"})


class LabHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _fmt, *_args):
        return

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path not in {"/", "/test-lab"}:
            self.send_error(404)
            return
        body = test_lab_page().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=11435)
    parser.add_argument("--token", required=True)
    parser.add_argument(
        "--browser-target",
        choices=("chrome", "chromium", "edge", "firefox", "extension", "comet"),
        default="comet",
    )
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.access_token = args.token
    server.browser_target = args.browser_target
    server.api_key = os.environ.get("OLLAMA_COMET_API_KEY", "")
    server.browser_broker = BrowserBroker()
    server.browser_controller = CDPBrowserController()
    server.agent_tasks = AgentTaskRegistry()
    server.native_agent_hub = NativeAgentHub()
    lab_server = ThreadingHTTPServer(("127.0.0.1", 11436), LabHandler)
    lab_thread = threading.Thread(target=lab_server.serve_forever, daemon=True)
    lab_thread.start()
    print(f"Ollama Comet bridge listening on http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
