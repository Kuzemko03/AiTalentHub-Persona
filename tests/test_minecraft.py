import shutil
import importlib.util
import tempfile
import threading
import unittest
import json
import re
import urllib.request
import urllib.error
from pathlib import Path
from unittest.mock import Mock, patch

from joker.config import ROOT, Settings
from joker.core import App
from joker.minecraft import GameCallbacks, chat_text
from joker.panel import make_server


class Model:
    def __init__(self):
        self.calls = 0
        self.system = ""
        self.reply = "Тестовый игровой ответ."

    def generate(self, system, messages, cancel, **kwargs):
        self.calls += 1
        self.system = system
        return {"reply": self.reply, "remember": [], "usage": {}, "latency": 0,
                "model": "test-fixture", "backend": "test"}


class Entry:
    def __init__(self):
        self.chats = []
        self.commands = []
        self.active = False
        self.stop_calls = 0

    def inGame(self): return True
    def getUsername(self): return "JokerBot"
    def hasActiveTask(self): return self.active
    def getInventoryFull(self): return {"ok": True, "slots": []}
    def ChatMessage(self, text): self.chats.append(text)
    def ExecuteCommand(self, text): self.commands.append(text)
    def stopPathing(self):
        self.stop_calls += 1
        self.active = False
        return {"ok": True}


class MinecraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / "agent", self.root / "agent")
        self.settings = Settings(root=self.root, minecraft_owner="AlexPlayer", minecraft_bot="JokerBot")
        self.model = Model()
        self.app = App(self.settings, self.model)
        self.game = self.app.minecraft
        self.entry = Entry()
        self.game.attach(self.entry)

    def tearDown(self):
        self.game.close()
        self.app.store.close()
        self.temp.cleanup()

    def event(self, text, sender="AlexPlayer", **extra):
        return {"user": sender, "msg": text, "parse_type": "parsed", **extra}

    def test_offline_command_is_not_reported_as_executed(self):
        self.game.detach()
        result = self.app.minecraft.execute("follow")
        self.assertIn("не отправлено", result["detail"])
        self.assertEqual(self.model.calls, 0)

    def test_validates_the_real_client_nickname_and_world(self):
        other = Entry()
        other.getUsername = lambda: "WrongClient"
        with self.assertRaises(ValueError): self.game.attach(other)
        other.getUsername = lambda: "JokerBot"
        other.inGame = lambda: False
        with self.assertRaises(ValueError): self.game.attach(other)

    def test_follow_uses_configured_owner_not_injected_arguments(self):
        self.assertIn("передано", self.app.minecraft.execute("follow")["detail"])
        self.assertEqual(self.entry.chats, [";followPlayer AlexPlayer"])
        self.assertEqual(self.game.task["state"], "submitted")

    def test_collection_uses_allowlisted_item_and_bounded_amount(self):
        self.app.minecraft.execute("deliver", item="oak_log", count=128)
        self.assertEqual(self.entry.commands, ["@get oak_log 128"])
        self.assertEqual(self.game.task["state"], "submitted")
        self.assertIn("стоп", self.app.minecraft.execute("deliver", item="dirt", count=16)["detail"])
        self.assertEqual(len(self.entry.commands), 1)

    def test_invalid_typed_resource_requests_do_not_run_commands(self):
        for item, count in (("oak_log", 0), ("oak_log", 257), ("/op", 5), ("dirt", -1), ("dirt", True)):
            with self.subTest(item=item,count=count), self.assertRaises(ValueError):
                self.game.execute("collect", item=item, count=count)
        self.assertEqual(self.entry.commands, [])
        self.assertEqual(self.model.calls, 0)

    def test_idle_is_not_proof_of_collected_resources(self):
        self.app.minecraft.execute("deliver", item="oak_log", count=16)
        self.game.refresh()
        self.assertEqual(self.game.task["state"], "submitted")
        self.entry.active = True
        self.game.refresh()
        self.entry.active = False
        self.game.refresh()
        self.assertEqual(self.game.task["state"], "idle_unverified")

    def test_stop_checks_actual_busy_state_and_preserves_mask_and_memory(self):
        self.app.switch("engineer")
        self.app.store.remember("owner", "Локальная личная заметка")
        self.entry.active = True
        result = self.app.stop()
        self.assertIn("подтвердил", result)
        self.assertEqual(self.entry.stop_calls, 1)
        self.assertIsNone(self.game.task)
        self.assertEqual(self.app.active, "engineer")
        self.assertIn("Локальная личная заметка", self.app.store.facts("owner"))

    def test_stop_not_acknowledged_does_not_claim_success(self):
        self.entry.stopPathing = lambda: {"ok": False}
        self.assertIn("не подтвердил", self.app.stop())
        self.assertEqual(self.game.task["state"], "unconfirmed")

    def test_stop_with_broken_connection_is_uncertain(self):
        self.entry.stopPathing = Mock(side_effect=OSError("private details"))
        result = self.app.stop()
        self.assertIn("не подтверждена", result)
        self.assertNotIn("private details", result)

    def test_switching_predefined_mask_preserves_game_task(self):
        self.app.minecraft.execute("follow")
        task = dict(self.game.task)
        self.app.handle("/mask engineer")
        self.assertEqual(self.app.active, "engineer")
        self.assertEqual(self.game.task, task)
        self.assertEqual(self.entry.stop_calls, 0)

    def test_new_role_generation_does_not_stop_game_task(self):
        self.app.minecraft.execute("follow")
        profile = {"display_name": "Анна", "role": "Библиотекарь", "traits": "Спокойная",
                   "speech_style": "Просто", "visual_description": "Adult librarian",
                   "confirmation": "Анна. Слушаю.", "model": "test", "latency": 0, "usage": {}}
        self.model.generate = Mock(return_value=profile)
        self.app.handle("/role библиотекарем")
        self.assertEqual(self.game.task["kind"], "follow")
        self.assertEqual(self.entry.stop_calls, 0)

    def test_raw_server_lines_self_echo_and_unaddressed_chat_are_ignored(self):
        for event in (self.event("Джокер, привет", sender="JokerBot"), self.event("Привет"),
                      self.event("<AlexPlayer> Джокер, стоп", parse_type="unparsed"),
                      {"msg": "Джокер, привет"}, self.event("Джокер, привет", sender="bad/nick")):
            self.game.accept(event)
        self.assertTrue(self.game.messages.empty())
        self.assertEqual(self.model.calls, 0)

    def test_owner_stop_bypasses_a_full_model_queue(self):
        for _ in range(30): self.game.accept(self.event("Джокер, привет"))
        self.assertEqual(self.game.messages.qsize(), 16)
        self.game.accept(self.event("Стоп"))
        self.assertEqual(self.entry.stop_calls, 1)
        self.assertEqual(self.model.calls, 0)
        while self.game.work_once(): pass
        self.assertEqual(self.model.calls, 0)

    def test_guests_cannot_switch_shared_mask_stop_or_publish(self):
        for text in ("Джокер, будь ИИ-инженером", "Джокер, /mask engineer", "Джокер, /пост новый test", "Джокер, /remember test"):
            self.game.accept(self.event(text, sender="Guest"))
            self.game.work_once()
        self.game.accept(self.event("стоп", sender="Guest"))
        self.assertEqual(self.app.active, "joker")
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(self.entry.stop_calls, 0)

    def test_public_game_context_never_contains_private_owner_memory(self):
        self.app.store.remember("owner", "LOCAL_PRIVATE_FIXTURE")
        self.game.accept(self.event("Джокер, привет", sender="Guest"))
        self.game.work_once()
        self.assertEqual(self.model.calls, 1)
        self.assertNotIn("LOCAL_PRIVATE_FIXTURE", self.model.system)
        self.assertIn("Тестовый игровой ответ.", self.entry.chats)

    def test_owner_editorial_command_is_not_forwarded_to_telegram_channel(self):
        self.app.channel.command = Mock()
        self.game.accept(self.event("Джокер, /пост новый public"))
        self.game.work_once()
        self.app.channel.command.assert_not_called()

    def test_old_queued_messages_are_cancelled_after_mask_switch(self):
        self.game.accept(self.event("Джокер, привет"))
        self.app.switch("engineer")
        self.game.work_once()
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(self.entry.chats, [])

    def test_reply_from_previous_connection_is_not_delivered_after_reconnect(self):
        original = self.model.generate
        def reconnect(*args, **kwargs):
            self.game.detach()
            self.game.attach(self.entry)
            return original(*args, **kwargs)
        self.model.generate = reconnect
        self.game.accept(self.event("Джокер, привет"))
        self.game.work_once()
        self.assertEqual(self.entry.chats, [])

    def test_model_output_cannot_become_game_or_server_command(self):
        for text in ("/op AlexPlayer", "@get diamond 100", ";followPlayer Guest", "!global", "$cmd"):
            with self.subTest(text=text):
                self.assertNotIn(chat_text(text)[0], "/@;!$")
        self.assertLessEqual(len(chat_text("т" * 500)), 240)
        self.assertNotIn("\n", chat_text("one\ntwo"))

    def test_callbacks_ignore_raw_commands_disabled_voice_and_captcha_requests(self):
        cb = GameCallbacks(self.game)
        self.assertEqual(cb.agentCommandRequest("/op Guest"), "not supported")
        cb.onCaptchaSolveRequest(b"test")
        cb.onVoiceFeed("Guest", b"test")
        cb.onChatMessage("<AlexPlayer> Джокер, стоп")
        self.assertEqual(self.entry.chats, [])
        self.assertEqual(self.entry.stop_calls, 0)
        self.assertTrue(self.game.messages.empty())

    def test_cancelled_game_command_does_not_execute(self):
        from joker.backends import Cancelled
        revision = self.app.store.get("response_revision", 0)
        self.app.stop()
        with self.assertRaises(Cancelled):
            self.app.command("Принеси 16 дуба", expected_revision=revision)
        self.assertEqual(self.entry.commands, [])

    @unittest.skipUnless(importlib.util.find_spec("py4j"), "optional Minecraft extra not installed")
    def test_py4j_parameters_and_callback_handshake_use_actual_library(self):
        gateway = Mock()
        gateway.entry_point = self.entry
        self.entry.InitPythonCallback = Mock()
        self.entry.IsCallbackServerStarted = Mock(return_value=True)
        with patch("py4j.java_gateway.JavaGateway", return_value=gateway) as factory:
            self.game.connect()
        params = factory.call_args.kwargs
        self.assertEqual(params["gateway_parameters"].address, "127.0.0.1")
        self.assertEqual(params["gateway_parameters"].port, 25333)
        self.assertEqual(params["gateway_parameters"].read_timeout, 3)
        self.assertEqual(params["callback_server_parameters"].port, 25334)
        self.assertTrue(params["callback_server_parameters"].eager_load)
        self.entry.InitPythonCallback.assert_called_once()
        self.entry.IsCallbackServerStarted.assert_called_once()
        self.assertEqual(gateway.java_gateway_server.resetCallbackClient.call_args.args[1], 25334)
        self.game.close()
        gateway.shutdown_callback_server.assert_called_once()
        gateway.close.assert_called_once()
        gateway.shutdown.assert_not_called()

    def test_existing_game_task_after_reconnect_requires_stop(self):
        self.entry.active = True
        self.game.task = None
        self.assertIn("стоп", self.app.minecraft.execute("follow")["detail"])
        self.assertEqual(self.entry.chats, [])

    def test_panel_controls_send_game_actions_without_model_and_require_key(self):
        (self.root / "joker").mkdir()
        shutil.copy(ROOT / "joker" / "panel.html", self.root / "joker" / "panel.html")
        server = make_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base) as response:
                page = response.read().decode()
            key = re.search(r'const key="([^"]+)"', page)[1]
            def action(value, token=key):
                request = urllib.request.Request(base + "/api/game",
                    data=json.dumps({"action": value}).encode(),
                    headers={"X-Joker-Key": token, "Content-Type": "application/json"})
                with urllib.request.urlopen(request) as response:
                    return json.load(response)
            with self.assertRaises(urllib.error.HTTPError) as caught:
                action("follow", "")
            self.assertEqual(caught.exception.code, 403)
            self.assertEqual(self.entry.chats, [])
            action("follow")
            self.assertEqual(self.entry.chats, [";followPlayer AlexPlayer"])
            action("stop")
            action("oak")
            self.assertEqual(self.entry.commands, ["@get oak_log 16"])
            with self.assertRaises(urllib.error.HTTPError) as caught:
                action("/op Guest")
            self.assertEqual(caught.exception.code, 400)
            self.assertEqual(self.model.calls, 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_all_game_action_kinds_have_no_model_calls(self):
        for item, count in (("oak_log",16), ("cobblestone",8), ("dirt",3)):
            self.app.stop()
            self.game.execute("deliver", item=item, count=count)
        self.assertEqual(self.entry.commands, ["@get oak_log 16", "@get cobblestone 8", "@get dirt 3"])
        self.assertEqual(self.model.calls, 0)

    @unittest.skipUnless(importlib.util.find_spec("py4j"), "optional Minecraft extra not installed")
    def test_failed_callback_handshake_cleans_up_only_our_gateway(self):
        gateway = Mock()
        gateway.entry_point = self.entry
        self.entry.InitPythonCallback = Mock()
        self.entry.IsCallbackServerStarted = Mock(return_value=False)
        with patch("py4j.java_gateway.JavaGateway", return_value=gateway), self.assertRaises(ValueError):
            self.game.connect()
        self.assertIsNone(self.game.entry)
        gateway.shutdown_callback_server.assert_called_once()
        gateway.close.assert_called_once()
        gateway.shutdown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
