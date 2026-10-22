"""Recover Desktop updates without silently replacing a user's custom CLI selection."""
import os
from pathlib import Path
from unittest.mock import patch

from joker.backends import CodexBackend, ModelError
from test_next_stage import Fixture


class CLIResolutionTests(Fixture):
    def setUp(self):
        super().setUp()
        self.base = self.root / "local-appdata"
        self.managed = self.base / "OpenAI/Codex/bin"
        env = patch.dict("os.environ", {"LOCALAPPDATA": str(self.base)})
        env.start()
        self.addCleanup(env.stop)
        which = patch("joker.backends.shutil.which", return_value=None)
        self.which = which.start()
        self.addCleanup(which.stop)

    def executable_file(self, version, modified):
        target = self.managed / version / "codex.exe"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"fixture-not-an-executable")
        os.utime(target, (modified, modified))
        return target

    def test_removed_managed_version_recovers_latest_installed_binary(self):
        self.executable_file("old", 1000)
        newest = self.executable_file("new", 2000)
        self.settings.codex_bin = str(self.managed / "removed" / "codex.exe")
        self.assertEqual(Path(CodexBackend(self.settings).executable()), newest)

    def test_existing_explicit_binary_remains_selected(self):
        selected = self.executable_file("old", 1000)
        self.executable_file("new", 2000)
        self.settings.codex_bin = str(selected)
        self.assertEqual(Path(CodexBackend(self.settings).executable()), selected)

    def test_missing_custom_path_is_not_replaced_by_another_installation(self):
        self.executable_file("new", 2000)
        self.settings.codex_bin = str(self.root / "my-custom-cli")
        with self.assertRaises(ModelError) as caught:
            CodexBackend(self.settings).executable()
        self.assertIn("CODEX_BIN", str(caught.exception))

    def test_path_has_priority_then_desktop_is_available_without_a_version_pin(self):
        newest = self.executable_file("new", 2000)
        self.settings.codex_bin = ""
        self.which.return_value = "fixture-path-cli"
        self.assertEqual(CodexBackend(self.settings).executable(), "fixture-path-cli")
        self.which.return_value = None
        self.assertEqual(Path(CodexBackend(self.settings).executable()), newest)

    def test_missing_cli_has_actionable_error(self):
        with self.assertRaises(ModelError):
            CodexBackend(self.settings).executable()
