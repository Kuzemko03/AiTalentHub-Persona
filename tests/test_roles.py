import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from joker.backends import Cancelled, ModelError
from joker.config import ROOT, Settings
from joker.core import App
from joker.roles import ROLE_SCHEMA, role_request
from joker.telegram_bot import TelegramRunner


PROFILE = {"display_name": "Марина", "role": "сварщица", "traits": "невозмутимая, внимательная к деталям",
           "speech_style": "простыми короткими фразами, с сухим юмором",
           "visual_description": "Fictional adult woman, 38, short red hair, brown eyes, sturdy build, navy work jacket, visible natural face.",
           "confirmation": "Марина. Что варить будем?"}


class RoleBackend:
    def __init__(self):
        self.calls = 0

    def generate(self, system, messages, cancel, *, schema=None):
        self.calls += 1
        self.schema, self.system, self.messages = schema, system, messages
        if schema == ROLE_SCHEMA:
            return {**PROFILE, "model": "test-fixture", "usage": {}, "latency": 0.01}
        return {"reply": "Показывай, что случилось.", "remember": [], "model": "test-fixture",
                "usage": {}, "latency": 0.01, "backend": "test"}


class RoleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / "agent", self.root / "agent")
        self.backend = RoleBackend()
        self.settings = Settings(root=self.root, owner_id=123)
        self.app = App(self.settings, self.backend)

    def tearDown(self):
        self.app.store.close()
        self.temp.cleanup()

    def test_free_role_description_persists_and_keeps_owner_memory(self):
        self.app.store.remember("owner", "Меня зовут Тимофей")
        description = "Джокер, стань сварщицей Мариной. Рыжая, 38 лет, спокойная, с сухим юмором."
        result = self.app.handle(description)
        self.assertEqual(result["reply"], PROFILE["confirmation"])
        self.assertEqual(self.backend.schema, ROLE_SCHEMA)
        self.assertIn("Рыжая", self.backend.messages[0]["content"])
        self.assertNotIn("Тимофей", self.backend.system + json.dumps(self.backend.messages, ensure_ascii=False))
        saved_id = self.app.active
        self.app.store.close()
        self.app = App(self.settings, self.backend)
        self.assertEqual(self.app.active, saved_id)
        self.assertEqual(self.app.masks[saved_id]["visual_description"], PROFILE["visual_description"])
        self.assertEqual(self.app.store.facts("owner"), ["Меня зовут Тимофей"])
        self.app.handle("Джокер, стань Мариной")
        self.assertEqual(self.backend.calls, 1)

    def test_known_role_is_instant_but_added_details_generate_new_profile(self):
        self.app.handle("Джокер, стань сантехником")
        self.assertEqual(self.app.active, "plumber")
        self.assertEqual(self.backend.calls, 0)
        self.app.handle("Джокер, стань сантехником Павлом, 30 лет, очень терпеливым")
        self.assertEqual(self.backend.calls, 1)

    def test_bad_profile_never_changes_current_role(self):
        self.app.switch("plumber")
        self.backend.generate = Mock(return_value={"display_name": "Недостаточно"})
        with self.assertRaises(ModelError):
            self.app.handle("стань смотрителем маяка")
        self.assertEqual(self.app.active, "plumber")
        self.assertFalse(self.app.store.get("custom_masks", {}))
        self.assertFalse(self.app.status()["generating"])

    def test_stop_during_role_creation_never_saves_or_switches_role(self):
        entered, release = threading.Event(), threading.Event()
        original = self.backend.generate
        def slow(*args, **kwargs):
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)
        self.backend.generate = slow
        failures = []
        def adopt():
            try:
                self.app.handle("стань сварщицей")
            except Cancelled:
                failures.append("cancelled")
        worker = threading.Thread(target=adopt)
        worker.start()
        self.assertTrue(entered.wait(3))
        self.app.stop()
        release.set()
        worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures, ["cancelled"])
        self.assertEqual(self.app.active, "joker")
        self.assertFalse(self.app.store.get("custom_masks", {}))

    def test_new_role_does_not_copy_old_assistant_voice(self):
        self.app.store.append("owner", "assistant", "Я персонаж в белой маске", "joker")
        self.app.store.append("owner", "user", "Меня зовут Тимофей", "joker")
        self.app.switch("housewife")
        self.app.reply("Кто ты?")
        self.assertNotIn("белой маске", json.dumps(self.backend.messages, ensure_ascii=False))
        self.assertIn("Меня зовут Тимофей", json.dumps(self.backend.messages, ensure_ascii=False))

    def test_role_request_is_anchored_and_preserves_description_case(self):
        self.assertIsNone(role_request("Что значит стань сантехником?"))
        self.assertEqual(role_request("Джокер, стань Павлом. Говори спокойно."), "Павлом. Говори спокойно")
        self.app.handle("Джокер, стань сварщицей", owner=False)
        self.assertEqual(self.backend.calls, 0)

    def test_telegram_new_role_is_deferred_and_error_is_delivered(self):
        api = Mock()
        runner = TelegramRunner(self.app, api)
        runner.accept({"update_id": 1, "message": {"chat": {"id": 123, "type": "private"},
                       "from": {"id": 123}, "text": "Джокер, стань смотрителем маяка"}})
        self.assertEqual(self.backend.calls, 0)
        self.backend.generate = Mock(side_effect=ModelError("Тестовая недоступность модели"))
        runner.work_once()
        api.send.assert_called_once()
        self.assertIn("недоступность", api.send.call_args.args[1])

    def test_portrait_paths_do_not_expose_other_files(self):
        from joker.roles import portrait_path
        self.assertIsNone(portrait_path(self.root, "../../.env"))
