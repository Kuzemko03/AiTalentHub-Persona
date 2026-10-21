import json
import importlib.util
import shutil
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from joker.config import ROOT, Settings
from joker.core import App
from joker.lifecycle import InstanceLock, snapshot


class Model:
    def __init__(self):
        self.calls = 0
        self.system = ""
        self.context = ""
        self.decision = {"action": "wait", "item": "oak_log", "count": 16, "seconds": 60, "note": "Проверю результат позже."}

    def generate(self, system, messages, cancel, **kwargs):
        self.calls += 1
        self.system, self.context = system, json.dumps(messages, ensure_ascii=False)
        if "schema" in kwargs:
            return dict(self.decision)
        return {"reply": "Привет из теста.", "remember": [], "usage": {}, "latency": 0,
                "model": "test", "backend": "test"}


class Game:
    def __init__(self):
        self.items = {0: ["oak_log", 64]}
        self.active = False
        self.commands, self.chats, self.clicks = [], [], []
        self.distance = 2
        self.menu = False
        self.stop_calls = 0
        self.ack = True

    def inGame(self): return True
    def getUsername(self): return "JokerBot"
    def hasActiveTask(self): return self.active
    def ChatMessage(self, text): self.chats.append(text)
    def ExecuteCommand(self, text): self.commands.append(text)
    def stopPathing(self):
        self.stop_calls += 1
        self.active = False
        return {"ok": True}
    def getInventoryFull(self):
        return {"ok": True, "slots": [{"index": i, "item": "minecraft:" + item,
            "count": count, "empty": count == 0} for i, (item, count) in self.items.items()]
            + [{"index": 35, "empty": True}]}
    def getPlayersInfo(self, count): return [{"name": "AlexPlayer", "distance": str(self.distance)}]
    def getOpenScreen(self): return {"open": self.menu}
    def lookAtPlayer(self, owner): return True
    def clickUiSlot(self, slot, button, action):
        self.clicks.append((slot, button, action))
        index = slot - 36 if slot >= 36 else slot
        self.items[index][1] -= self.items[index][1] if button == 1 else 1
        return self.ack


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / "agent", self.root / "agent")
        self.settings = Settings(root=self.root, minecraft_owner="AlexPlayer", minecraft_bot="JokerBot")
        self.model = Model()
        self.app = App(self.settings, self.model)
        self.entry = Game()
        self.app.minecraft.attach(self.entry)

    def tearDown(self):
        self.app.runtime.close()
        self.app.voice.close()
        self.app.minecraft.close()
        self.app.store.close()
        self.temp.cleanup()


class DeliveryTests(Fixture):
    def test_come_stops_only_after_observing_owner_nearby(self):
        self.entry.distance = 20
        self.app.minecraft.execute("come")
        self.app.minecraft.refresh()
        self.assertEqual(self.entry.stop_calls, 0)
        self.entry.distance = 2
        self.app.minecraft.refresh()
        self.app.minecraft.refresh()
        self.assertEqual(self.app.minecraft.task["state"], "arrived")
        self.assertEqual(self.entry.stop_calls, 1)

    def advance_to_handover(self):
        for _ in range(4): self.app.minecraft.refresh()

    def test_collect_confirms_inventory_without_giving_it_away(self):
        self.app.minecraft.execute("collect", item="oak_log", count=16)
        self.app.minecraft.refresh()
        self.assertEqual(self.app.minecraft.task["state"], "collected")
        self.assertEqual(self.entry.items[0][1], 64)
        self.assertEqual(self.entry.clicks, [])

    def test_delivery_of_16_never_throws_a_stack_of_64(self):
        self.app.minecraft.execute("deliver", item="oak_log", count=16)
        self.advance_to_handover()
        for _ in range(18): self.app.minecraft.refresh()
        self.assertEqual(self.app.minecraft.task["state"], "delivered_unverified")
        self.assertEqual(self.entry.items[0][1], 48)
        self.assertEqual(len(self.entry.clicks), 16)
        self.assertTrue(all(c == (36, 0, "THROW") for c in self.entry.clicks))
        self.assertIn("подбор", self.app.minecraft.task["detail"])

    def test_whole_stack_handover_and_main_slot_mapping(self):
        self.entry.items = {9: ["cobblestone", 64], 10: ["cobblestone", 64]}
        self.app.minecraft.execute("deliver", item="cobblestone", count=128)
        for _ in range(8): self.app.minecraft.refresh()
        self.assertEqual(self.entry.clicks, [(9, 1, "THROW"), (10, 1, "THROW")])
        self.assertEqual(self.app.minecraft.task["state"], "delivered_unverified")

    def test_owner_out_of_range_and_open_menu_prevent_drops(self):
        self.app.minecraft.execute("deliver", item="oak_log", count=16)
        self.advance_to_handover()
        self.entry.distance = 20
        self.app.minecraft.refresh()
        self.assertEqual(self.entry.clicks, [])
        self.entry.distance = 2
        self.entry.menu = True
        self.app.minecraft.refresh()
        self.assertEqual(self.entry.clicks, [])

    def test_lost_drop_ack_never_retries(self):
        self.app.minecraft.execute("deliver", item="oak_log", count=16)
        self.advance_to_handover()
        self.entry.ack = False
        self.app.minecraft.refresh()
        for _ in range(5): self.app.minecraft.refresh()
        self.assertEqual(len(self.entry.clicks), 1)
        self.assertEqual(self.app.minecraft.task["state"], "unconfirmed")

    def test_inventory_does_not_change_after_ack_never_retries(self):
        self.app.minecraft.execute("deliver", item="oak_log", count=16)
        self.advance_to_handover()
        self.entry.clickUiSlot = Mock(return_value=True)
        self.app.minecraft.refresh()
        self.app.minecraft.task["pending_drop"]["at"] -= 4
        self.app.minecraft.refresh()
        self.assertEqual(self.entry.clickUiSlot.call_count, 1)
        self.assertEqual(self.app.minecraft.task["state"], "unconfirmed")

    def test_stop_cancels_the_remaining_handover(self):
        self.app.minecraft.execute("deliver", item="oak_log", count=16)
        self.advance_to_handover()
        self.app.minecraft.refresh()
        self.app.stop()
        for _ in range(3): self.app.minecraft.refresh()
        self.assertEqual(len(self.entry.clicks), 1)
        self.assertIsNone(self.app.minecraft.task)

    def test_time_limit_stops_without_claiming_delivery(self):
        self.app.minecraft.execute("deliver", item="oak_log", count=16)
        self.app.minecraft.task["started_at"] -= 901
        self.app.minecraft.refresh()
        self.assertEqual(self.app.minecraft.task["state"], "failed")
        self.assertEqual(self.entry.clicks, [])

    @unittest.skipUnless(importlib.util.find_spec("py4j"), "optional Minecraft extra not installed")
    def test_wrong_client_cannot_autojoin_server(self):
        self.settings.minecraft_autoconnect = True
        self.settings.minecraft_server = "example.invalid:25565"
        self.entry.getUsername = lambda: "OtherAccount"
        self.entry.ConnectToServer = Mock()
        gateway = Mock(entry_point=self.entry)
        with patch("py4j.java_gateway.JavaGateway", return_value=gateway), self.assertRaises(ValueError):
            self.app.minecraft.connect()
        gateway.entry_point.ConnectToServer.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("py4j"), "optional Minecraft extra not installed")
    def test_autojoin_is_explicit_rate_limited_and_disabled_in_sleep(self):
        self.app.minecraft.detach()
        self.entry.inGame = lambda: False
        self.entry.ConnectToServer = Mock()
        self.settings.minecraft_server = "example.invalid:25565"
        gateway = Mock(entry_point=self.entry)
        with patch("py4j.java_gateway.JavaGateway", return_value=gateway):
            with self.assertRaises(ValueError): self.app.minecraft.connect()
            self.entry.ConnectToServer.assert_not_called()
            self.settings.minecraft_autoconnect = True
            with self.assertRaises(ValueError): self.app.minecraft.connect()
            self.entry.ConnectToServer.assert_called_once_with("example.invalid:25565")
            with self.assertRaises(ValueError): self.app.minecraft.connect()
            self.assertEqual(self.entry.ConnectToServer.call_count, 1)
            self.app.minecraft.next_join = 0
            self.app.handle("/сон")
            with self.assertRaises(ValueError): self.app.minecraft.connect()
            self.assertEqual(self.entry.ConnectToServer.call_count, 1)


class RuntimeTests(Fixture):
    def enable(self):
        self.app.handle("/цель Собери дуб для дома")
        self.app.handle("/автономность вкл")

    def test_default_off_and_disconnected_game_use_no_model(self):
        self.app.runtime.tick()
        self.enable()
        self.app.minecraft.detach()
        self.app.runtime.tick()
        self.assertEqual(self.model.calls, 0)

    def test_sleep_persists_plan_and_skips_models_until_wake(self):
        self.enable()
        self.app.handle("/сон")
        self.app.runtime.tick()
        result = self.app.handle("Привет")
        self.assertIn("сплю", result["reply"])
        self.assertEqual(self.model.calls, 0)
        from joker.runtime import Runtime
        restored = Runtime(self.app)
        self.assertTrue(restored.sleeping())
        self.assertEqual(restored.state["goal"], "Собери дуб для дома")
        self.app.handle("/проснись")
        self.assertFalse(self.app.runtime.sleeping())
        self.assertFalse(self.app.runtime.state["enabled"])

    def test_agent_decision_uses_validated_tools_and_keeps_private_memory_out(self):
        self.app.store.remember("owner", "PRIVATE_RUNTIME_FIXTURE")
        self.model.decision.update(action="collect", count=8)
        self.enable()
        self.app.runtime.tick()
        self.assertEqual(self.entry.commands, ["@get oak_log 8"])
        self.assertNotIn("PRIVATE_RUNTIME_FIXTURE", self.model.system + self.model.context)
        self.assertEqual(len(self.app.store.get("runtime_plan")), 1)
        self.assertTrue(self.app.store.get("runtime")["pending_action"])

    def test_restart_does_not_replay_pending_side_effect(self):
        self.model.decision.update(action="deliver")
        self.enable()
        self.app.runtime.tick()
        from joker.runtime import Runtime
        restored = Runtime(self.app)
        self.assertFalse(restored.state["enabled"])
        restored.tick()
        self.assertEqual(len(self.entry.commands), 1)

    def test_invalid_model_action_has_no_side_effects(self):
        self.model.decision.update(action="/op AlexPlayer")
        self.enable()
        self.app.runtime.tick()
        self.assertEqual(self.entry.commands, [])
        self.assertEqual(self.entry.chats, [])

    def test_stop_during_planning_cancels_its_action(self):
        original = self.model.generate
        def stopped(*args, **kwargs):
            result = original(*args, **kwargs)
            self.app.stop()
            return result
        self.model.generate = stopped
        self.model.decision.update(action="follow")
        self.enable()
        self.app.runtime.tick()
        self.assertEqual(self.entry.chats, [])
        self.assertFalse(self.app.runtime.state["enabled"])

    def test_daily_budget_and_model_lane_bound_autonomy(self):
        self.settings.runtime_daily_requests = 1
        self.enable()
        self.app.generation.acquire()
        self.app.runtime.tick()
        self.app.generation.release()
        self.assertEqual(self.model.calls, 0)
        self.app.runtime.next_tick = 0
        self.app.runtime.tick()
        self.app.runtime.next_tick = 0
        self.app.runtime.tick()
        self.assertEqual(self.model.calls, 1)

    def test_ambiguous_delivery_pauses_autonomy(self):
        self.enable()
        self.app.minecraft.task = {"kind": "deliver", "state": "delivered_unverified"}
        self.app.runtime.tick()
        self.assertFalse(self.app.runtime.state["enabled"])
        self.assertEqual(self.model.calls, 0)

    def test_quiet_hours_cross_midnight_and_preserve_resume_preference(self):
        self.settings.quiet_hours = "23:00-08:00"
        self.settings.utc_offset = 3
        now = datetime(2026, 10, 21, 21, tzinfo=timezone.utc).timestamp()
        self.assertEqual(self.app.runtime.quiet_remaining(now), 8 * 3600)
        self.enable()
        self.app.runtime.tick(now)
        self.assertTrue(self.app.runtime.sleeping())
        self.assertTrue(self.app.runtime.state["resume_enabled"])
        self.assertEqual(self.entry.stop_calls, 2)  # Setting the goal and sleeping both stop.

    def test_sleep_prevents_comment_worker_and_channel_scheduler(self):
        self.app.handle("/сон")
        self.app.comments.work_once(Mock())
        self.assertEqual(self.app.channel.next_draft()["action"], "idle")
        self.assertEqual(self.model.calls, 0)


class LifecycleTests(Fixture):
    def test_only_one_instance_owns_the_runtime_directory(self):
        first, second = InstanceLock(self.root), InstanceLock(self.root)
        first.acquire()
        try:
            with self.assertRaises(ValueError): second.acquire()
        finally:
            first.close()
        second.acquire()
        second.close()

    def test_backup_can_restore_memory_and_plan_without_copying_env(self):
        self.app.store.remember("owner", "Тестовая память")
        self.app.handle("/цель Тестовая цель")
        path = snapshot(self.app.store, self.root)
        self.assertTrue(path.is_relative_to(self.root / "data" / "backups"))
        with closing(sqlite3.connect(path)) as restored:
            self.assertEqual(restored.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(restored.execute("SELECT fact FROM facts").fetchone()[0], "Тестовая память")
            state = json.loads(restored.execute("SELECT value FROM settings WHERE key='runtime'").fetchone()[0])
            self.assertEqual(state["goal"], "Тестовая цель")
        self.assertEqual(list(path.parent.glob(".env")), [])


if __name__ == "__main__": unittest.main()
