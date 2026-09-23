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
    def create(self, _task_id):
        return threading.Event()

    def remove(self, _task_id):
        return None


class FakeBroker:
    def __init__(self):
        self.calls = []

    def connected(self):
        return True

    def execute(self, method, arguments, allow_sensitive=False):
        self.calls.append((method, arguments, allow_sensitive))
        return {"message": "extension action complete"}


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


if __name__ == "__main__":
    unittest.main()
