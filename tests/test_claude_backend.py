import io
import json
import threading
import unittest
from unittest import mock

from joker.backends import ClaudeBackend, ModelError, make_backend
from joker.config import Settings


class FakeProcess:
    def __init__(self, stdout, returncode=0):
        self.stdout_text, self.returncode = stdout, returncode
        self.stderr = io.TextIOWrapper(io.BytesIO(b"\x91"), encoding="utf-8")
        self.input = None

    def communicate(self, prompt):
        self.input = prompt
        return self.stdout_text, ""

    def kill(self):
        pass


class ClaudeBackendTest(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(backend="claude", claude_bin="claude")
        self.backend = ClaudeBackend(self.settings)
        self.calls = []

    def run_with(self, payload, returncode=0):
        process = FakeProcess(json.dumps(payload), returncode)

        def popen(command, **kwargs):
            self.calls.append((command, kwargs))
            return process

        with mock.patch("joker.backends.subprocess.Popen", popen), \
                mock.patch.object(ClaudeBackend, "executable", return_value="claude"):
            return self.backend.generate("Правила", [{"role": "user", "content": "привет"}], threading.Event()), process

    def test_selected_by_setting(self):
        self.assertIsInstance(make_backend(self.settings), ClaudeBackend)
        self.assertEqual(self.settings.active_model, "claude-haiku-5-5")
        self.assertEqual(self.settings.active_reasoning, "low")

    def test_structured_output_and_locked_down_command(self):
        result, process = self.run_with({"is_error": False, "result": "",
                                         "structured_output": {"reply": "Здорово", "remember": [], "tool": None},
                                         "usage": {"input_tokens": 2, "cache_read_input_tokens": 10,
                                                   "output_tokens": 5}})
        self.assertEqual(result["reply"], "Здорово")
        self.assertEqual(result["backend"], "claude")
        self.assertEqual(result["usage"], {"input_tokens": 12, "output_tokens": 5})
        command, kwargs = self.calls[0]
        self.assertEqual(command[command.index("--tools") + 1], "")
        self.assertIn("--strict-mcp-config", command)
        self.assertEqual(command[command.index("--model") + 1], "claude-haiku-5-5")
        self.assertNotIn("привет", " ".join(command))
        self.assertIn("привет", process.input)
        self.assertNotIn("TELEGRAM_BOT_TOKEN", kwargs["env"])

    def test_errors_are_classified(self):
        with self.assertRaisesRegex(ModelError, "лимит Claude"):
            self.run_with({"is_error": True, "result": "Usage limit reached"}, returncode=1)
        with self.assertRaises(ModelError):
            self.run_with({"is_error": False, "result": "не json"})


if __name__ == "__main__":
    unittest.main()
