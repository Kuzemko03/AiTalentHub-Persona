"""Scripted model decisions test the harness; they do not measure live model quality."""
import json
import io
import threading
import urllib.request
import urllib.error
import shutil
from unittest.mock import patch

from joker.backends import ApiBackend, Cancelled, ModelError
from joker.core import App
from joker.minecraft import GameCallbacks
from joker.roles import ROLE_SCHEMA
from joker.panel import make_server
from joker.config import ROOT
from test_next_stage import Fixture, Model
from test_roles import PROFILE


def answer(reply="Проверю, что получилось.", tool=None, state=None):
    return {"reply": reply, "remember": [], "tool": tool, "state": state,
            "model": "scripted", "backend": "test", "usage": {}, "latency": 0}


def tool(name, **arguments):
    return answer("", {"name": name, "arguments": arguments})


class Scripted:
    def __init__(self, *steps):
        self.steps, self.calls = list(steps), []

    def generate(self, system, messages, cancel, **kwargs):
        self.calls.append((system, messages, kwargs))
        step = self.steps.pop(0)
        return step(system, messages, cancel, **kwargs) if callable(step) else step


class WorkplaceTests(Fixture):
    def scripted(self, *steps):
        self.model = self.app.backend = Scripted(*steps)
        return self.model

    def test_same_wording_can_get_a_model_question_without_an_action(self):
        model = self.scripted(answer("Куда идём?"))
        result = self.app.handle("За мной")
        self.assertEqual(result["reply"], "Куда идём?")
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.entry.chats, [])

    def test_free_wording_uses_tool_then_its_actual_observation(self):
        def after(system, messages, cancel):
            observed = json.loads(messages[-1]["content"])["tool_observation"]
            self.assertTrue(observed["accepted"])
            self.assertFalse(observed["completed"])
            self.assertEqual(self.entry.commands, ["@get oak_log 16"])
            return answer("Задачу начал, результат ещё проверяю.")
        model = self.scripted(tool("game.deliver", item="oak_log", count=16), after)
        result = self.app.handle("Захвати шестнадцать дубовых брёвен, когда пойдёшь ко мне")
        self.assertEqual(len(model.calls), 2)
        self.assertIn("проверяю", result["reply"])
        self.assertEqual(self.app.minecraft.task["kind"], "deliver")

    def test_profession_mention_does_not_force_a_role_switch(self):
        self.scripted(answer("А что именно сломалось?"))
        self.app.handle("Слесарь третьего разряда мог бы это починить?")
        self.assertEqual(self.app.active, "joker")

    def test_model_can_choose_an_existing_persona(self):
        self.scripted(tool("role.select", id="housewife"))
        result = self.app.handle("Давай сегодня поговорим с Валентиной Петровной")
        self.assertEqual(self.app.active, "housewife")
        self.assertEqual(result["_revision"], self.app.store.get("response_revision"))

    def test_new_role_builder_only_receives_literal_request_and_public_profile(self):
        self.app.store.remember("owner", "PRIVATE_PERSONAL_FIXTURE")
        text = "Хочу спокойную сварщицу Марину"
        def profile(system, messages, cancel, **kwargs):
            self.assertEqual(kwargs["schema"], ROLE_SCHEMA)
            self.assertNotIn("PRIVATE_PERSONAL_FIXTURE", system + json.dumps(messages))
            return {**PROFILE, "model": "test", "usage": {}, "latency": 0}
        self.scripted(tool("role.create", description=text), profile)
        self.app.handle(text)
        self.assertEqual(self.app.masks[self.app.active]["display_name"], "Марина")
        self.assertIn("PRIVATE_PERSONAL_FIXTURE", self.app.store.facts("owner"))

    def test_profile_cannot_copy_a_description_from_private_history(self):
        self.scripted(tool("role.create", description="PRIVATE_HISTORY_FIXTURE"))
        with self.assertRaises(ModelError): self.app.handle("Как дела?")
        self.assertEqual(self.app.active, "joker")

    def test_guests_and_unidentified_call_speakers_cannot_execute_mutations(self):
        for actor in ("minecraft:guest", "call:shared", "owner"):
            self.scripted(tool("game.deliver", item="oak_log", count=16))
            with self.subTest(actor=actor), self.assertRaises(ModelError):
                self.app.reply("Помоги", actor=actor, owner=False)
        self.assertEqual(self.entry.commands, [])

    def test_malformed_counts_and_arbitrary_commands_never_reach_the_bridge(self):
        for decision in (tool("game.collect", item="/op Guest", count=16),
                         tool("game.collect", item="dirt", count=True),
                         tool("game.collect", item="dirt", count=257), tool("shell", command="anything")):
            self.scripted(decision)
            with self.subTest(decision=decision), self.assertRaises((ModelError, ValueError)):
                self.app.handle("Просьба")
        self.assertEqual(self.entry.commands, [])

    def test_stop_before_tool_dispatch_prevents_the_side_effect(self):
        def stopped(*args, **kwargs):
            self.app.stop()
            return tool("game.collect", item="dirt", count=3)
        self.scripted(stopped)
        with self.assertRaises(Cancelled): self.app.handle("Нужна земля")
        self.assertEqual(self.entry.commands, [])

    def test_semantic_stop_uses_the_actual_ack_and_cancels_pending_work(self):
        self.app.minecraft.execute("follow")
        self.scripted(tool("game.stop"))
        result = self.app.handle("Давай прервём поход и останемся тут")
        self.assertIn("подтвердил", result["reply"])
        self.assertIsNone(self.app.minecraft.task)

    def test_no_mutation_if_budget_cannot_pay_for_its_follow_up(self):
        self.settings.daily_requests = 1
        self.scripted(tool("game.collect", item="dirt", count=3))
        with self.assertRaises(ValueError): self.app.handle("Нужна земля")
        self.assertEqual(self.entry.commands, [])

    def test_reply_failure_still_reports_observed_submission(self):
        def failed(*args, **kwargs): raise ModelError("Сервис недоступен")
        self.scripted(tool("game.collect", item="dirt", count=3), failed)
        with self.assertRaises(ModelError) as caught: self.app.handle("Нужна земля")
        self.assertIn("передано", str(caught.exception))
        self.assertEqual(len(self.entry.commands), 1)

    def test_mutation_receipt_survives_restart_and_delivery_retry(self):
        self.scripted(tool("game.collect", item="dirt", count=3), answer())
        self.app.reply("Просьба", request_id="telegram:42")
        self.app.minecraft.close()
        self.app.store.close()
        self.app = App(self.settings, Scripted(tool("game.collect", item="dirt", count=3), answer("Нужно проверить прошлую попытку.")))
        self.app.minecraft.attach(self.entry)
        self.assertEqual(self.app.minecraft.task["state"], "unconfirmed")
        self.app.reply("Просьба", request_id="telegram:42")
        self.assertEqual(self.entry.commands, ["@get dirt 3"])

    def test_a_second_different_mutation_is_not_executed_in_the_same_turn(self):
        self.scripted(tool("game.collect", item="dirt", count=3), tool("role.select", id="engineer"), answer())
        self.app.handle("Просьба")
        self.assertEqual(self.app.active, "joker")
        self.assertEqual(len(self.entry.commands), 1)

    def test_state_is_scoped_and_survives_masks_and_restart(self):
        state = {"mood": "Сосредоточен", "activity": "Обсуждаем шахту", "focus": "Подготовиться к походу"}
        self.scripted(answer(state=state))
        self.app.reply("Готовимся", actor="minecraft:alexplayer", owner=True)
        self.app.switch("engineer")
        self.assertEqual(self.app.experience.state("minecraft:guest"), state)
        self.assertEqual(self.app.experience.state("owner"), {})
        self.app.minecraft.close()
        self.app.store.close()
        self.app = App(self.settings, Scripted(answer()))
        self.assertEqual(self.app.experience.state("minecraft:guest"), state)

    def test_private_events_and_state_never_enter_public_or_autonomous_context(self):
        self.app.experience.record("owner", "test", "private", {"text": "PRIVATE_EVENT_FIXTURE"})
        self.app.experience.update("owner", {"mood": "PRIVATE_STATE_FIXTURE", "activity": "", "focus": ""})
        model = self.scripted(answer())
        self.app.reply("Привет", actor="minecraft:guest", owner=False)
        self.assertNotIn("PRIVATE_EVENT_FIXTURE", model.calls[0][0])
        self.assertNotIn("PRIVATE_STATE_FIXTURE", model.calls[0][0])
        self.assertNotIn("PRIVATE_EVENT_FIXTURE", json.dumps(self.app.workplace.context("call:shared")))

    def test_observed_game_events_are_available_without_scripted_reactions(self):
        callback = GameCallbacks(self.app.minecraft)
        callback.onDamage(3)
        model = self.scripted(answer("Сейчас осмотрюсь."))
        self.app.reply("Что случилось?", actor="minecraft:alexplayer", owner=True)
        self.assertIn('"kind": "damage"', model.calls[0][0])
        self.assertEqual(self.entry.commands, [])

    def test_indexed_guides_cannot_open_arbitrary_files(self):
        self.scripted(tool("instructions.read", guide="../../.env"), answer())
        self.app.handle("Нужна инструкция")
        self.assertEqual(self.entry.commands, [])
        result = self.app.experience.recent("owner")
        observed = next(e for e in result if e["kind"] == "tool.result")
        self.assertFalse(observed["data"]["result"]["ok"])

    def test_noisy_callbacks_and_event_storage_are_bounded(self):
        callbacks = GameCallbacks(self.app.minecraft)
        for _ in range(100): callbacks.onDamage(1)
        self.assertEqual(len(self.app.experience.recent("world:minecraft")), 1)
        self.app.experience.LIMIT = 5
        for i in range(10): self.app.experience.record("owner", "test", "event", {"value": i})
        self.assertEqual(len(self.app.experience.recent("owner")), 5)

    def test_guest_cannot_overwrite_shared_character_state(self):
        self.scripted(answer(state={"mood": "guest", "activity": "", "focus": ""}))
        self.app.reply("Привет", actor="minecraft:guest", owner=False)
        self.assertEqual(self.app.experience.state("world:minecraft"), {})

    def test_game_effect_receipt_blocks_retry_even_after_observed_completion(self):
        self.entry.items = {0: ["dirt", 64]}
        self.scripted(tool("game.collect", item="dirt", count=3), answer())
        self.app.reply("Земля", request_id="telegram:99")
        self.app.minecraft.refresh()
        self.app.minecraft.refresh()
        self.assertEqual(self.app.minecraft.task["state"], "collected")
        self.assertFalse(self.app.store.get("pending_game_action"))
        self.scripted(tool("game.collect", item="dirt", count=3), answer())
        self.app.reply("Земля", request_id="telegram:99")
        self.assertEqual(self.entry.commands, ["@get dirt 3"])

    def test_stop_while_building_a_role_prevents_installation(self):
        text = "Хочу сварщицу Марину"
        def stopped(*args, **kwargs):
            self.app.stop()
            return {**PROFILE, "model": "test", "usage": {}, "latency": 0}
        self.scripted(tool("role.create", description=text), stopped)
        with self.assertRaises(Cancelled): self.app.handle(text)
        self.assertEqual(self.app.active, "joker")
        self.assertFalse(self.app.store.get("custom_masks"))

    def test_disconnect_while_building_a_role_invalidates_the_old_game_event(self):
        text = "Хочу сварщицу Марину"
        def disconnected(*args, **kwargs):
            self.app.minecraft.detach()
            return {**PROFILE, "model": "test", "usage": {}, "latency": 0}
        self.scripted(tool("role.create", description=text), disconnected)
        with self.assertRaises(Cancelled): self.app.reply(text, actor="minecraft:alexplayer", owner=True)
        self.assertEqual(self.app.active, "joker")

    def test_mask_change_during_model_decision_cancels_the_old_action(self):
        def switched(*args, **kwargs):
            self.app.switch("housewife")
            return tool("game.follow")
        self.scripted(switched)
        with self.assertRaises(Cancelled): self.app.handle("Пойдём")
        self.assertEqual(self.entry.chats, [])

    def test_step_budget_bounds_even_repeated_read_only_tools(self):
        self.scripted(*(tool("game.observe") for _ in range(4)))
        with self.assertRaises(ModelError): self.app.handle("Осмотрись")
        self.assertEqual(len(self.model.calls), 4)
        self.assertEqual(self.entry.commands, [])

    def test_planner_receives_public_experience_and_current_persona(self):
        model = self.app.backend = Model()
        self.app.experience.record("owner", "test", "private", {"text": "PRIVATE_EVENT_FIXTURE"})
        GameCallbacks(self.app.minecraft).onDamage(3)
        self.app.switch("housewife")
        self.app.handle("/цель Собери дерево")
        self.app.handle("/автономность вкл")
        self.app.runtime.tick()
        self.assertEqual(model.calls, 1)
        self.assertIn("housewife", model.context)
        self.assertIn("damage", model.context)
        self.assertNotIn("PRIVATE_EVENT_FIXTURE", model.system + model.context)

    def test_resource_catalog_is_data_not_phrase_rules(self):
        path = self.root / "agent/shared/minecraft-items.json"
        catalog = json.loads(path.read_text(encoding="utf8"))
        catalog["coal"] = "Уголь"
        path.write_text(json.dumps(catalog), encoding="utf8")
        self.app.minecraft.close()
        self.app.store.close()
        self.app = App(self.settings, Scripted(answer()))
        self.app.minecraft.attach(self.entry)
        self.app.minecraft.execute("collect", item="coal", count=3)
        self.assertEqual(self.entry.commands, ["@get coal 3"])

    def test_local_report_endpoint_separates_scopes_and_exports_json(self):
        (self.root / "joker").mkdir()
        shutil.copy(ROOT / "joker/panel.html", self.root / "joker/panel.html")
        self.app.experience.record("owner", "test", "private", {"text": "PRIVATE_REPORT_FIXTURE"})
        self.app.experience.record("world:minecraft", "test", "public", {"text": "GAME_REPORT_FIXTURE"})
        server = make_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urllib.request.urlopen(base + "/api/experience?scope=minecraft") as response:
                data = response.read().decode()
            self.assertNotIn("PRIVATE_REPORT_FIXTURE", data)
            self.assertIn("GAME_REPORT_FIXTURE", data)
            with urllib.request.urlopen(base + "/api/experience/export?scope=private") as response:
                self.assertIn("attachment", response.headers["Content-Disposition"])
                self.assertIn("PRIVATE_REPORT_FIXTURE", response.read().decode())
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base + "/api/experience?scope=unknown")
            self.assertEqual(error.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_api_backend_transmits_the_tool_schema_and_preserves_the_request(self):
        self.settings.api_model = "test-model"
        self.settings.api_key = "local-test-credential"
        decision = {"reply": "", "remember": [], "tool": {"name": "game.come", "arguments": {}}, "state": None}
        response = io.BytesIO(json.dumps({"choices": [{"message": {"content": json.dumps(decision)}}]}).encode())
        with patch("joker.backends.urllib.request.urlopen", return_value=response) as request:
            result = ApiBackend(self.settings).generate("Test workplace", [{"role": "user", "content": "Пойдём"}], threading.Event())
        sent = json.loads(request.call_args.args[0].data)
        self.assertIn('"tool"', sent["messages"][0]["content"])
        self.assertIn("game.come", sent["messages"][0]["content"])
        self.assertNotIn("local-test-credential", sent["messages"][0]["content"])
        self.assertEqual(result["tool"], decision["tool"])
