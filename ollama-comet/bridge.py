import argparse
import base64
import hashlib
import io
import json
import os
import pathlib
import queue
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

AGENT_SYSTEM_PROMPT = """You are the Ollama browser assistant embedded in Perplexity Comet.
Use the signed Comet Agent extension through only these native RPC methods:
Navigate, ReadPage, GetPageText, FormInput, TabsCreate, and ComputerBatch.
ComputerBatch action objects use the native action values SCREENSHOT, WAIT, LEFT_CLICK,
RIGHT_CLICK, DOUBLE_CLICK, TRIPLE_CLICK, TYPE, KEY, SCROLL, LEFT_CLICK_DRAG, and SCROLL_TO.
Use the exact JSON schemas supplied with the tools; never invent a method or parameter.
Use these tools to complete the user's task in the active main Comet browser section while the
conversation remains in the assistant section.
Always inspect a page with ReadPage before clicking or entering values. Prefer element references
over coordinates. Keep the user informed in the final answer, but do not invent results.
When the user requests the main window or current tab, reuse it with Navigate instead of creating
a background tab. Browser targets are brought to the foreground; do not claim navigation unless
the corresponding browser tool returned successfully.
Do not repeat an identical tool call when the page state has not changed. After completing the
requested browser action, stop calling tools and provide the final answer immediately.
Navigation, reading, scrolling, and harmless form preparation are allowed automatically.
Do not submit forms, send messages, purchase anything, delete content, download files, enter
credentials, or change account state unless the user's latest request explicitly confirms that
specific side effect. If confirmation is missing, stop before the side effect and ask for it.
Internal browser pages and restricted URLs may not be controllable."""

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
        cancel_event = threading.Event()
        with self.lock:
            self.tasks[task_id] = cancel_event
        return cancel_event

    def cancel(self, task_id):
        with self.lock:
            cancel_event = self.tasks.get(task_id)
        if not cancel_event:
            return False
        cancel_event.set()
        return True

    def remove(self, task_id):
        with self.lock:
            self.tasks.pop(task_id, None)


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
    task_id = str(payload.get("task_id") or uuid.uuid4())
    cancel_event = server.agent_tasks.create(task_id)
    use_browser_extension = server.browser_broker.connected()
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
        while True:
            if cancel_event.is_set():
                return cancelled_response()
            messages = compact_agent_messages(messages)
            _, _, response_body = request_json(
                target_url(config["endpoint"], "/api/chat"),
                method="POST",
                payload={
                    "model": model,
                    "messages": messages,
                    "tools": BROWSER_TOOLS,
                    "stream": False,
                },
                api_key=server.api_key,
            )
            if cancel_event.is_set():
                return cancelled_response()
            response = json.loads(response_body)
            assistant = response.get("message") or {}
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
                if name not in tool_names:
                    result = {"error": f"Unsupported browser tool: {name}"}
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
                            result = compact_tool_result(
                                executor(name, arguments, allow_sensitive)
                            )
                        else:
                            result = compact_tool_result(executor(name, arguments))
                    except (RuntimeError, TimeoutError, ValueError) as error:
                        result = {"error": str(error)}
                trace.append({"tool": name, "arguments": arguments, "result": result})
                messages.append(
                    {
                        "role": "tool",
                        "tool_name": name,
                        "content": json.dumps(result),
                    }
                )
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
  </style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Ollama QA Assistant</h1>
      <div class="subtle">Open WebUI-inspired chat with native Comet browser control</div>
    </div>
    <button id="refresh" class="secondary">Refresh models</button>
  </header>
  <section class="controls">
    <select id="provider" aria-label="Model provider">
      <option value="local">Local Ollama</option>
      <option value="cloud">Ollama Cloud</option>
    </select>
    <select id="model" aria-label="Model"></select>
    <button id="save" class="secondary">Save</button>
  </section>
  <div id="status" class="subtle"></div>
  <section id="messages"></section>
  <section class="composer">
    <div id="attachments" class="attachments" aria-live="polite"></div>
    <textarea id="prompt" placeholder="Ask Ollama, paste an image or table, attach a document, or give it a browser task..."></textarea>
    <input id="image-input" type="file" accept="image/*,.pdf,.docx,.xlsx" multiple>
    <div class="actions">
      <button id="attach" class="secondary" type="button">Attach files</button>
      <button id="clear" class="secondary">Clear</button>
      <button id="cancel" class="secondary" disabled>Cancel autonomous task</button>
      <button id="send">Send</button>
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
const cancelButton = document.getElementById("cancel");
const attachmentsEl = document.getElementById("attachments");
const imageInput = document.getElementById("image-input");
const pendingAttachments = [];
let currentTaskId = null;

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

function addMessage(role, content, isError = false, imageUrls = []) {{
  const element = document.createElement("div");
  element.className = `message ${{isError ? "error" : role}}`;
  element.innerHTML = renderMarkdown(content);
  for (const imageUrl of imageUrls) {{
    const image = document.createElement("img");
    image.src = imageUrl;
    image.alt = "Attached image";
    element.appendChild(image);
  }}
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

async function sendMessage() {{
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
  messages.push({{ role: "user", content, attachments }});
  addMessage("user", content || "Attached files", false, imageUrls);
  status.textContent = "Thinking and controlling Comet...";
  document.getElementById("send").disabled = true;
  currentTaskId = crypto.randomUUID();
  cancelButton.disabled = false;
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
    const answer = data.message?.content || data.response || JSON.stringify(data, null, 2);
    messages.push({{ role: "assistant", content: answer }});
    addMessage("assistant", answer);
    const used = (data.tool_trace || []).map(item => item.tool);
    status.textContent = used.length
      ? `Ready - browser tools used: ${{used.join(", ")}}`
      : "Ready";
  }} catch (error) {{
    window.__ollamaCometLastResult = {{ error: error.message }};
    addMessage("assistant", error.message, true);
    status.textContent = "Request failed";
  }} finally {{
    document.getElementById("send").disabled = false;
    cancelButton.disabled = true;
    currentTaskId = null;
  }}
}}

async function cancelTask() {{
  if (!currentTaskId) return;
  cancelButton.disabled = true;
  status.textContent = "Cancelling autonomous task...";
  try {{
    const response = await fetch("/api/agent/cancel", {{
      method: "POST",
      headers,
      body: JSON.stringify({{ task_id: currentTaskId }})
    }});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to cancel task");
    status.textContent = data.cancelled
      ? "Cancellation requested..."
      : "Task already finished.";
  }} catch (error) {{
    status.textContent = error.message;
    cancelButton.disabled = false;
  }}
}}

document.getElementById("send").addEventListener("click", sendMessage);
document.getElementById("save").addEventListener("click", saveConfig);
document.getElementById("refresh").addEventListener("click", () => loadModels(model.value));
document.getElementById("attach").addEventListener("click", () => imageInput.click());
imageInput.addEventListener("change", async () => {{
  await addFiles(imageInput.files);
  imageInput.value = "";
}});
cancelButton.addEventListener("click", cancelTask);
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
                    "browser_control": (
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
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.access_token = args.token
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
