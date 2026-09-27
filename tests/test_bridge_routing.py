import importlib.util
import json
import pathlib
import threading
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "autonomous_browser_bridge",
    ROOT / "ollama-comet" / "bridge.py",
)
bridge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bridge)


class FakeTasks:
    def __init__(self):
        self._events = {}
        self._lock = threading.Lock()
        self.seen = []

    def create(self, task_id):
        with self._lock:
            self._events[task_id] = []
        return threading.Event()

    def log(self, task_id, event):
        with self._lock:
            self._events.setdefault(task_id, []).append(event)
            self.seen.append(event)

    def progress(self, task_id, after=0):
        with self._lock:
            events = self._events.get(task_id, [])
            return {
                "task_id": task_id,
                "active": task_id in self._events,
                "index": len(events),
                "events": list(events[after:]),
            }

    def remove(self, task_id):
        with self._lock:
            self._events.pop(task_id, None)


class FakeBroker:
    def __init__(self):
        self.calls = []

    def connected(self):
        return True

    def execute(self, method, arguments, allow_sensitive=False):
        self.calls.append((method, arguments, allow_sensitive))
        return {"message": "extension action complete"}


class ScriptedBroker:
    def __init__(self, results=None):
        self.calls = []
        self._results = results or {}

    def connected(self):
        return True

    def execute(self, method, arguments, allow_sensitive=False):
        self.calls.append((method, arguments, allow_sensitive))
        return self._results.get(method, {"message": "extension action complete"})


class ForbiddenNativeHub:
    def create_session(self, *_args):
        raise AssertionError("native Comet session should not be created")

    def remove_session(self, *_args):
        raise AssertionError("native Comet session should not be removed")


class ForbiddenBrowserController:
    def start_native_agent(self, *_args):
        raise AssertionError("Comet bootstrap should not run")

    def close_target(self, target_id):
        if target_id is not None:
            raise AssertionError("unexpected Comet target")


class FakeServer:
    server_port = 11435
    access_token = "test-token"
    api_key = ""
    browser_target = "chrome"

    def __init__(self):
        self.browser_broker = FakeBroker()
        self.browser_controller = ForbiddenBrowserController()
        self.native_agent_hub = ForbiddenNativeHub()
        self.agent_tasks = FakeTasks()


class BridgeRoutingTests(unittest.TestCase):
    def setUp(self):
        self.original_load_config = bridge.load_config
        self.original_request_json = bridge.request_json
        bridge.load_config = lambda: {
            "endpoint": "http://127.0.0.1:11434",
            "model": "test-model",
        }

    def tearDown(self):
        bridge.load_config = self.original_load_config
        bridge.request_json = self.original_request_json

    def test_extension_connection_bypasses_comet_and_executes_tools(self):
        responses = iter(
            [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "Navigate",
                                    "arguments": {"url": "https://example.com"},
                                }
                            }
                        ],
                    },
                    "model": "test-model",
                },
                {
                    "message": {
                        "role": "assistant",
                        "content": "Completed in the extension.",
                    },
                    "model": "test-model",
                },
            ]
        )

        def request_json(*_args, **_kwargs):
            return 200, "application/json", json.dumps(next(responses)).encode()

        bridge.request_json = request_json
        server = FakeServer()
        result = bridge.run_agent(
            server,
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": "Open example.com"}],
            },
        )

        self.assertEqual(result["message"]["content"], "Completed in the extension.")
        self.assertEqual(
            server.browser_broker.calls,
            [("Navigate", {"url": "https://example.com"}, False)],
        )

        progress = server.agent_tasks.progress(result["task_id"])
        self.assertFalse(progress["active"])
        event_types = [event["type"] for event in server.agent_tasks.seen]
        self.assertEqual(event_types, ["phase", "tool", "tool_result", "phase"])
        self.assertEqual(server.agent_tasks.seen[2]["tool"], "Navigate")
        self.assertEqual(server.agent_tasks.seen[2]["ok"], True)

    def test_agent_task_registry_progress(self):
        registry = bridge.AgentTaskRegistry()
        registry.create("t1")
        registry.log("t1", {"type": "phase", "text": "Thinking (round 1)"})
        registry.log("t1", {"type": "tool", "tool": "Navigate", "detail": "{}"})

        active = registry.progress("t1", after=0)
        self.assertTrue(active["active"])
        self.assertEqual(active["index"], 2)
        self.assertEqual(len(active["events"]), 2)

        resumed = registry.progress("t1", after=1)
        self.assertEqual([event["type"] for event in resumed["events"]], ["tool"])
        self.assertEqual(resumed["index"], 2)

        registry.remove("t1")
        done = registry.progress("t1")
        self.assertFalse(done["active"])
        self.assertEqual(done["events"], [])

    def test_browser_broker_command_round_trip(self):
        broker = bridge.BrowserBroker()
        outcome = {}

        def execute():
            outcome["value"] = broker.execute(
                "ReadPage",
                {"filter": "interactive"},
                timeout=2,
            )

        worker = threading.Thread(target=execute)
        worker.start()
        command = broker.next_command(timeout=1)
        self.assertEqual(command["method"], "ReadPage")
        broker.complete(
            command["id"],
            {"ok": True, "value": {"title": "Test page"}},
        )
        worker.join(timeout=2)

        self.assertEqual(outcome["value"], {"title": "Test page"})

    def test_comet_target_does_not_select_generic_extension(self):
        server = FakeServer()
        server.browser_target = "comet"
        self.assertFalse(bridge.should_use_browser_extension(server))

    def test_chromium_target_selects_connected_extension(self):
        server = FakeServer()
        self.assertTrue(bridge.should_use_browser_extension(server))
        server.browser_target = "chromium"
        self.assertTrue(bridge.should_use_browser_extension(server))

    @staticmethod
    def _tool_call_response(name, arguments):
        return {
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"function": {"name": name, "arguments": arguments}}
                ],
            },
            "model": "test-model",
        }

    @staticmethod
    def _final_response(content):
        return {
            "message": {"role": "assistant", "content": content},
            "model": "test-model",
        }

    def _run_with_responses(self, server, responses, config=None):
        iterator = iter(responses)
        payloads = []

        def request_json(*_args, **_kwargs):
            payloads.append(_kwargs.get("payload"))
            return 200, "application/json", json.dumps(next(iterator)).encode()

        bridge.request_json = request_json
        result = bridge.run_agent(
            server,
            {
                "model": "test-model",
                "messages": [{"role": "user", "content": "Do the task"}],
            },
        )
        return result, payloads

    def test_extension_dispatches_tabs_list(self):
        server = FakeServer()
        server.browser_broker = ScriptedBroker(
            {
                "TabsList": {
                    "tab_context": {
                        "current_tab_id": 7,
                        "executed_on_tab_id": 7,
                        "available_tabs": [{"tab_id": 7, "title": "Docs"}],
                        "tab_count": 1,
                    },
                    "message": "listed 1 tab",
                }
            }
        )
        result, _payloads = self._run_with_responses(
            server,
            [
                self._tool_call_response("TabsList", {}),
                self._final_response("Found 1 tab."),
            ],
        )

        self.assertEqual(server.browser_broker.calls, [("TabsList", {}, False)])
        self.assertEqual(result["tool_trace"][0]["tool"], "TabsList")
        self.assertEqual(
            result["tool_trace"][0]["result"]["tab_context"]["tab_count"], 1
        )
        event_types = [event["type"] for event in server.agent_tasks.seen]
        self.assertEqual(event_types, ["phase", "tool", "tool_result", "phase"])
        self.assertEqual(server.agent_tasks.seen[2]["tool"], "TabsList")

    def test_extension_dispatches_evaluate_js(self):
        server = FakeServer()
        server.browser_broker = ScriptedBroker(
            {
                "EvaluateJS": {
                    "tab_context": {"current_tab_id": 3, "tab_count": 1},
                    "result": "Example Domain",
                }
            }
        )
        result, _payloads = self._run_with_responses(
            server,
            [
                self._tool_call_response(
                    "EvaluateJS", {"expression": "document.title"}
                ),
                self._final_response("The title is Example Domain."),
            ],
        )

        self.assertEqual(
            server.browser_broker.calls,
            [("EvaluateJS", {"expression": "document.title"}, False)],
        )
        self.assertEqual(
            result["tool_trace"][0]["result"]["result"], "Example Domain"
        )
        event_types = [event["type"] for event in server.agent_tasks.seen]
        self.assertEqual(event_types, ["phase", "tool", "tool_result", "phase"])
        self.assertEqual(server.agent_tasks.seen[2]["tool"], "EvaluateJS")

    def test_vault_dispatch_uses_vault_module(self):
        dispatch_calls = []

        class FakeVaultModule:
            @staticmethod
            def dispatch_tool(name, arguments, config, allow_sensitive, executor):
                dispatch_calls.append(
                    {
                        "name": name,
                        "arguments": arguments,
                        "config": config,
                        "allow_sensitive": allow_sensitive,
                        "executor": executor,
                    }
                )
                return {"credentials": ["o'brien---smith-jane--ontario-ca"]}

        original_load_vault = bridge.load_vault_module
        bridge.load_vault_module = lambda: FakeVaultModule()
        try:
            server = FakeServer()
            config = dict(self.setUpConfig())
            config.update(
                {
                    "vault_uri": "https://qa-dev-app.vault.azure.net",
                    "vault_name": "qa-dev-app",
                }
            )
            bridge.load_config = lambda: config
            result, payloads = self._run_with_responses(
                server,
                [
                    self._tool_call_response("VaultListCredentials", {}),
                    self._final_response("Listed the vault credentials."),
                ],
            )
        finally:
            bridge.load_vault_module = original_load_vault

        self.assertEqual(server.browser_broker.calls, [])
        self.assertEqual(len(dispatch_calls), 1)
        call = dispatch_calls[0]
        self.assertEqual(call["name"], "VaultListCredentials")
        self.assertEqual(call["arguments"], {})
        self.assertEqual(call["config"]["vault_name"], "qa-dev-app")
        self.assertFalse(call["allow_sensitive"])
        self.assertTrue(callable(call["executor"]))
        self.assertEqual(
            result["tool_trace"][0]["result"],
            {"credentials": ["o'brien---smith-jane--ontario-ca"]},
        )
        event_types = [event["type"] for event in server.agent_tasks.seen]
        self.assertEqual(event_types, ["phase", "tool", "tool_result", "phase"])
        self.assertEqual(server.agent_tasks.seen[2]["tool"], "VaultListCredentials")

    def test_vault_tools_rejected_without_vault_config(self):
        def forbidden_load_vault():
            raise AssertionError("vault module must not load without config")

        original_load_vault = bridge.load_vault_module
        bridge.load_vault_module = forbidden_load_vault
        try:
            server = FakeServer()
            result, payloads = self._run_with_responses(
                server,
                [
                    self._tool_call_response("VaultListCredentials", {}),
                    self._final_response("Vault is not available."),
                ],
            )
        finally:
            bridge.load_vault_module = original_load_vault

        self.assertEqual(server.browser_broker.calls, [])
        tool_messages = [
            message
            for message in payloads[1]["messages"]
            if message.get("role") == "tool"
        ]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn(
            "Unsupported browser tool: VaultListCredentials",
            tool_messages[0]["content"],
        )
        self.assertEqual(
            result["tool_trace"][0]["result"],
            {"error": "Unsupported browser tool: VaultListCredentials"},
        )

    def test_prompt_lists_new_tools_and_vault_policy(self):
        prompt = bridge.AGENT_SYSTEM_PROMPT
        for name in (
            "TabsList",
            "EvaluateJS",
            "VaultConnect",
            "VaultListCredentials",
            "VaultGetCredential",
            "VaultLogin",
            "VaultResetPassword",
        ):
            self.assertIn(name, prompt)
        self.assertIn("submit=true", prompt)
        self.assertIn("confirm=true", prompt)
        self.assertIn("masked", prompt)

    def test_ado_dispatch_uses_ado_module(self):
        dispatch_calls = []

        class FakeAdoModule:
            @staticmethod
            def dispatch_tool(name, arguments, config, allow_sensitive, executor):
                dispatch_calls.append(
                    {
                        "name": name,
                        "arguments": arguments,
                        "config": config,
                        "allow_sensitive": allow_sensitive,
                        "executor": executor,
                    }
                )
                return {"value": [{"id": 12, "name": "Smoke Plan"}]}

        original_load_ado = bridge.load_ado_module
        bridge.load_ado_module = lambda: FakeAdoModule()
        try:
            server = FakeServer()
            config = dict(self.setUpConfig())
            config.update({"ado_org": "org", "ado_pat": "secret-pat-value"})
            bridge.load_config = lambda: config
            result, payloads = self._run_with_responses(
                server,
                [
                    self._tool_call_response("AdoListTestPlans", {}),
                    self._final_response("Listed the test plans."),
                ],
            )
        finally:
            bridge.load_ado_module = original_load_ado

        self.assertEqual(server.browser_broker.calls, [])
        self.assertEqual(len(dispatch_calls), 1)
        call = dispatch_calls[0]
        self.assertEqual(call["name"], "AdoListTestPlans")
        self.assertEqual(call["arguments"], {})
        self.assertEqual(call["config"]["ado_org"], "org")
        self.assertFalse(call["allow_sensitive"])
        self.assertIsNone(call["executor"])
        self.assertEqual(
            result["tool_trace"][0]["result"],
            {"value": [{"id": 12, "name": "Smoke Plan"}]},
        )
        event_types = [event["type"] for event in server.agent_tasks.seen]
        self.assertEqual(event_types, ["phase", "tool", "tool_result", "phase"])
        self.assertEqual(server.agent_tasks.seen[2]["tool"], "AdoListTestPlans")

    def test_ado_tools_rejected_without_ado_config(self):
        def forbidden_load_ado():
            raise AssertionError("ado module must not load without config")

        original_load_ado = bridge.load_ado_module
        bridge.load_ado_module = forbidden_load_ado
        try:
            server = FakeServer()
            result, payloads = self._run_with_responses(
                server,
                [
                    self._tool_call_response("AdoListTestPlans", {}),
                    self._final_response("Azure DevOps is not available."),
                ],
            )
        finally:
            bridge.load_ado_module = original_load_ado

        self.assertEqual(server.browser_broker.calls, [])
        tool_messages = [
            message
            for message in payloads[1]["messages"]
            if message.get("role") == "tool"
        ]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn(
            "Unsupported browser tool: AdoListTestPlans",
            tool_messages[0]["content"],
        )
        self.assertEqual(
            result["tool_trace"][0]["result"],
            {"error": "Unsupported browser tool: AdoListTestPlans"},
        )

    def test_prompt_lists_ado_tools(self):
        prompt = bridge.AGENT_SYSTEM_PROMPT
        for name in (
            "AdoConnect",
            "AdoListRepositories",
            "AdoFindTestFiles",
            "AdoInspectTestFile",
            "AdoListTestPlans",
            "AdoListSuites",
            "AdoListTestPoints",
            "AdoListTestRuns",
            "AdoRunGraceTest",
            "AdoPublishTestRun",
        ):
            self.assertIn(name, prompt)
        self.assertIn("Azure DevOps is configured", prompt)
        self.assertIn("need confirm=true", prompt)

    def test_vision_images_collected_and_compacted(self):
        server = FakeServer()
        image_data = "data:image/png;base64," + "A" * 400
        server.browser_broker = ScriptedBroker(
            {
                "ComputerBatch": {
                    "tab_context": {"current_tab_id": 5, "tab_count": 1},
                    "message": "screenshot captured",
                    "screenshot": image_data,
                }
            }
        )
        config = dict(self.setUpConfig())
        config["enable_vision"] = "true"
        bridge.load_config = lambda: config
        result, payloads = self._run_with_responses(
            server,
            [
                self._tool_call_response(
                    "ComputerBatch",
                    {"actions": [{"action": "SCREENSHOT"}]},
                ),
                self._final_response("Screenshot attached."),
            ],
        )

        tool_messages = [
            message
            for message in payloads[1]["messages"]
            if message.get("role") == "tool"
        ]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(len(tool_messages[0]["images"]), 1)
        self.assertEqual(tool_messages[0]["images"][0], "A" * 400)
        self.assertNotIn(image_data, tool_messages[0]["content"])
        self.assertEqual(
            result["tool_trace"][0]["result"]["screenshot"],
            f"[binary image omitted: {len(image_data)} characters]",
        )

    def test_vision_disabled_returns_no_images(self):
        server = FakeServer()
        server.browser_broker = ScriptedBroker(
            {
                "ComputerBatch": {
                    "message": "screenshot captured",
                    "screenshot": "data:image/png;base64," + "A" * 400,
                }
            }
        )
        config = dict(self.setUpConfig())
        config["enable_vision"] = "false"
        bridge.load_config = lambda: config
        result, payloads = self._run_with_responses(
            server,
            [
                self._tool_call_response(
                    "ComputerBatch",
                    {"actions": [{"action": "SCREENSHOT"}]},
                ),
                self._final_response("Screenshot captured."),
            ],
        )

        tool_messages = [
            message
            for message in payloads[1]["messages"]
            if message.get("role") == "tool"
        ]
        self.assertEqual(tool_messages[0].get("images"), None)
        self.assertNotIn("A" * 400, tool_messages[0]["content"])
        self.assertEqual(
            result["tool_trace"][0]["result"]["screenshot"],
            "[binary image omitted: 422 characters]",
        )

    def setUpConfig(self):
        return {
            "endpoint": "http://127.0.0.1:11434",
            "model": "test-model",
        }


if __name__ == "__main__":
    unittest.main()
