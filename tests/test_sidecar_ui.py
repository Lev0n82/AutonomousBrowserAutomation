import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]

spec = importlib.util.spec_from_file_location(
    "autonomous_browser_bridge", ROOT / "ollama-comet" / "bridge.py"
)
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)


class ComposerQueueUITests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = bridge.html_page("test-token")

    def test_multistate_send_icons_present(self):
        for marker in (
            "M8 5v14l11-7z",  # play
            "M6 6h12v12H6z",  # stop
            "executing",
            "stop-pulse",
            "updateSendButton",
        ):
            self.assertIn(marker, self.html)

    def test_queue_ui_markers_present(self):
        for marker in (
            'id="queue"',
            'id="queue-panel"',
            "queue-list",
            "queue-now",
            "queueMessage",
            "renderQueue",
            "sendQueuedNow",
            "advanceQueue",
            "RESUME_PREFIX",
        ):
            self.assertIn(marker, self.html)

    def test_old_cancel_control_removed(self):
        self.assertNotIn('id="cancel"', self.html)
        self.assertNotIn("cancelButton", self.html)
        self.assertNotIn("cancelTask", self.html)


if __name__ == "__main__":
    unittest.main()