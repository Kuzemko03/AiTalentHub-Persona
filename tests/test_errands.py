"""Queue lifecycle and observations, using a scripted model and bridge."""
import json
import threading
import urllib.error
import urllib.request
from unittest.mock import patch

from joker.backends import ModelError
from joker.core import App
from joker.errands import QueueConflict
from joker.panel import make_server
from test_next_stage import Fixture
from test_workplace import Scripted, answer, tool


def step(action="collect", item="oak_log", count=16, note="Для стройки"):
    return dict(action=action, item=item, count=count, note=note)


class ErrandTests(Fixture):
    def create(self, *steps, title="Подготовка к стройке"):
        return self.app.errands.create(title, list(steps or [step(), step("come", "", 1)]))

    def resume(self):
        q = self.app.errands.status()
        return self.app.errands.control(q["id"], q["version"], "resume")

    def tick(self, action="start", **overrides):
        s = self.app.errands.current()
        result = dict(action=action, step_id=s["id"], seconds=60, note="По наблюдаемой ситуации")
        result.update(overrides)
        self.app.backend = Scripted({**result, "usage": {}, "latency": 0, "backend": "test", "model": "scripted"})
        self.app.runtime.next_tick = 0
        self.app.runtime.tick()

    def test_conversation_saves_model_selected_steps_without_starting_them(self):
        self.app.backend = Scripted(tool("queue.create", title="Два поручения", steps=[step(), step("come", "", 1)]),
                                    answer("Проверь список, потом запусти его."))
        reply = self.app.handle("Сначала шестнадцать дубовых брёвен, потом подойди ко мне")
        self.assertIn("Проверь", reply["reply"])
        self.assertEqual(self.app.errands.status()["status"], "paused")
        self.assertEqual(self.entry.commands + self.entry.chats, [])
        self.app.runtime.tick()
        self.assertEqual(self.entry.commands, [])

    def test_malformed_steps_and_unsupported_actions_are_rejected_before_bridge(self):
        for steps in ([], [step()] * 9, [step(count=True)], [step(item="/op AlexPlayer")],
                      [step("follow")], [step("come")], [{**step(), "command": "anything"}]):
            with self.subTest(steps=steps), self.assertRaises(ValueError):
                self.app.errands.create("Список", steps)
        self.assertEqual(self.entry.commands + self.entry.chats, [])
        self.assertIsNone(self.app.errands.status())

    def test_matching_observations_advance_two_steps_in_order(self):
        q = self.create()
        self.resume()
        self.tick()
        task_id = self.app.minecraft.task["id"]
        self.assertEqual(self.app.errands.current()["status"], "running")
        self.assertEqual(self.entry.chats, [])
        self.app.minecraft.refresh()
        self.assertEqual(self.app.errands.current()["action"], "come")
        self.tick()
        self.assertNotEqual(task_id, self.app.minecraft.task["id"])
        self.assertEqual(self.entry.commands, ["@get oak_log 16"])
        self.assertEqual(self.entry.chats, [";followPlayer AlexPlayer"])
        self.app.minecraft.refresh()
        self.app.minecraft.refresh()
        self.assertEqual(self.app.errands.status()["status"], "finished")
        self.assertTrue(all(s["status"] == "done" for s in self.app.errands.status()["steps"]))
        self.assertEqual(q["steps"][0]["status"], "pending")  # snapshots are detached

    def test_no_extra_model_calls_or_dispatches_while_a_step_is_running(self):
        self.create()
        self.resume()
        self.tick()
        for _ in range(3):
            self.app.runtime.next_tick = 0
            self.app.runtime.tick()
        self.assertEqual(len(self.app.backend.calls), 1)
        self.assertEqual(len(self.entry.commands), 1)

    def test_model_can_wait_or_pause_instead_of_starting(self):
        self.create()
        self.resume()
        self.tick("wait", seconds=300)
        self.assertGreater(self.app.runtime.next_tick, 0)
        self.assertEqual(self.app.errands.current()["status"], "pending")
        self.tick("pause")
        self.assertEqual(self.app.errands.status()["status"], "paused")
        self.assertEqual(self.entry.commands, [])

    def test_model_cannot_skip_a_step_or_claim_it_done(self):
        self.create()
        self.resume()
        self.tick(step_id=self.app.errands.status()["steps"][1]["id"])
        self.assertEqual(self.entry.commands, [])
        self.tick("done")
        self.assertEqual(self.entry.commands, [])
        self.assertEqual(self.app.errands.current()["status"], "pending")

    def test_unrelated_task_cannot_complete_the_step(self):
        self.create()
        self.resume()
        self.tick()
        self.app.minecraft.task.update(id="unrelated", state="collected")
        self.app.minecraft.trace_task()
        self.assertEqual(self.app.errands.current()["status"], "review")
        self.assertEqual(self.app.errands.status()["status"], "paused")

    def test_busy_bridge_cannot_advance_a_collected_step(self):
        self.create()
        self.resume()
        self.tick()
        task = self.app.minecraft.task
        task["state"] = "collected"
        self.app.errands.observe(task, True)
        self.assertEqual(self.app.errands.current()["status"], "running")
        self.app.errands.observe(task, False)
        self.assertEqual(self.app.errands.current()["action"], "come")

    def test_owner_leaving_during_arrival_does_not_complete_the_step(self):
        self.create(step("come", "", 1))
        self.resume()
        self.tick()
        self.app.minecraft.refresh()
        self.entry.distance = 20
        self.app.minecraft.refresh()
        self.assertEqual(self.app.errands.current()["status"], "review")
        self.assertEqual(self.app.minecraft.task["state"], "unconfirmed")

    def test_come_timeout_pauses_the_queue(self):
        self.create(step("come", "", 1))
        self.resume()
        self.tick()
        self.app.minecraft.task["started_at"] -= 901
        self.app.minecraft.refresh()
        self.assertEqual(self.app.errands.current()["status"], "review")
        self.assertEqual(self.app.minecraft.task["state"], "failed")

    def test_unverified_delivery_requires_specific_manual_review_and_resume(self):
        self.create(step("deliver"), step("come", "", 1))
        self.resume()
        self.tick()
        for _ in range(25):
            self.app.minecraft.refresh()
        q = self.app.errands.status()
        self.assertEqual(q["status"], "paused")
        self.assertEqual(q["steps"][0]["status"], "review")
        self.assertEqual(len(self.entry.clicks), 16)
        with self.assertRaises(ValueError):
            self.resume()
        with self.assertRaises(ValueError):
            self.app.errands.confirm(q["id"], q["version"], q["steps"][0]["id"])
        self.app.stop()
        with self.assertRaises(QueueConflict):
            self.app.errands.confirm(q["id"], q["version"], q["steps"][0]["id"])
        q = self.app.errands.status()
        with self.assertRaises(ValueError):
            self.app.errands.confirm(q["id"], q["version"], q["steps"][1]["id"])
        self.app.errands.confirm(q["id"], q["version"], q["steps"][0]["id"])
        self.assertEqual(self.app.errands.status()["status"], "paused")
        self.assertIn("вручную", self.app.errands.status()["steps"][0]["result"])
        self.app.runtime.tick()
        self.assertEqual(self.entry.chats, [";followPlayer AlexPlayer"])
        self.resume()
        self.tick()
        self.assertEqual(len(self.entry.clicks), 16)
        self.assertEqual(self.app.errands.current()["action"], "come")

    def test_lost_handover_ack_never_retries(self):
        self.create(step("deliver"))
        self.resume()
        self.tick()
        self.entry.ack = False
        for _ in range(10):
            self.app.minecraft.refresh()
        for _ in range(3):
            self.app.runtime.next_tick = 0
            self.app.runtime.tick()
        self.assertEqual(self.app.errands.current()["status"], "review")
        self.assertEqual(len(self.entry.clicks), 1)
        self.assertEqual(self.entry.commands, ["@get oak_log 16"])

    def test_stop_preserves_queue_and_marks_inflight_step_for_review(self):
        self.create()
        self.resume()
        self.tick()
        self.app.stop()
        self.assertEqual(self.app.errands.status()["status"], "paused")
        self.assertEqual(self.app.errands.current()["status"], "review")
        self.assertIsNone(self.app.minecraft.task)
        with self.assertRaises(ValueError):
            self.resume()

    def test_restart_pauses_dispatched_step_without_replay(self):
        self.create()
        self.resume()
        self.tick()
        self.app.store.close()  # Simulate process loss before a clean Stop.
        self.app = App(self.settings, self.model)
        self.app.minecraft.attach(self.entry)
        self.assertEqual(self.app.errands.status()["status"], "paused")
        self.assertEqual(self.app.errands.current()["status"], "review")
        self.app.runtime.tick()
        self.assertEqual(self.entry.commands, ["@get oak_log 16"])
        with self.assertRaises(ValueError):
            self.resume()

    def test_crash_between_durable_intent_and_bridge_call_needs_review(self):
        self.create()
        self.resume()
        with patch.object(self.app.workplace, "execute", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                self.tick()
        self.assertEqual(self.app.errands.current()["status"], "review")
        self.assertEqual(self.entry.commands, [])
        with self.assertRaises(ValueError):
            self.resume()

    def test_disconnect_pauses_pending_or_running_steps(self):
        self.create()
        self.resume()
        self.app.minecraft.detach()
        self.assertEqual(self.app.errands.status()["status"], "paused")
        self.assertEqual(self.app.errands.current()["status"], "pending")
        self.app.minecraft.attach(self.entry)
        self.resume()
        self.tick()
        self.app.minecraft.detach()
        self.assertEqual(self.app.errands.current()["status"], "review")

    def test_stale_queue_controls_cannot_resume_or_cancel_a_changed_list(self):
        q = self.create()
        self.resume()
        for action in ("resume", "cancel", "pause"):
            with self.subTest(action=action), self.assertRaises(QueueConflict):
                self.app.errands.control(q["id"], q["version"], action)
        self.assertEqual(self.app.errands.status()["status"], "active")

    def test_separate_game_action_cannot_overwrite_an_unfinished_queue(self):
        self.create()
        result = self.app.minecraft.execute("deliver", item="oak_log", count=16)
        self.assertFalse(result["accepted"])
        q = self.app.errands.status()
        self.app.errands.control(q["id"], q["version"], "cancel")
        self.assertTrue(self.app.minecraft.execute("collect", item="oak_log", count=16)["accepted"])

    def test_goal_autonomy_does_not_interleave_with_paused_queue(self):
        self.create()
        self.app.runtime.state.update(enabled=True, goal="Другая цель")
        self.app.runtime.tick()
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(self.entry.commands, [])

    def test_stop_and_role_switch_invalidate_a_late_model_decision(self):
        for operation in (self.app.stop, lambda: self.app.switch("housewife")):
            if self.app.errands.owns_game():
                q = self.app.errands.status()
                self.app.errands.control(q["id"], q["version"], "cancel")
            self.create()
            self.resume()
            started, release = threading.Event(), threading.Event()
            decision = dict(action="start", step_id=self.app.errands.current()["id"], seconds=60, note="Сейчас")
            def slow(*args, **kwargs):
                started.set()
                release.wait(3)
                return decision
            self.app.backend = Scripted(slow)
            self.app.runtime.next_tick = 0
            thread = threading.Thread(target=self.app.runtime.tick)
            thread.start()
            self.assertTrue(started.wait(3))
            operation()
            release.set()
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertEqual(self.entry.commands, [])
            self.assertEqual(self.app.errands.current()["status"], "pending")

    def test_daily_budgets_apply_before_any_dispatch(self):
        self.create()
        self.resume()
        self.settings.daily_requests = 0
        self.tick()
        self.assertEqual(self.entry.commands, [])
        self.assertEqual(self.app.backend.calls, [])
        self.settings.daily_requests = 100
        self.settings.runtime_daily_requests = 0
        self.tick()
        self.assertEqual(self.app.backend.calls, [])

    def test_guests_calls_and_owner_false_cannot_create_or_control_queues(self):
        for actor in ("minecraft:guest", "call:shared", "owner"):
            self.app.backend = Scripted(tool("queue.create", title="Поручения", steps=[step()]))
            with self.subTest(actor=actor), self.assertRaises(ModelError):
                self.app.reply("Сделай список", actor=actor, owner=False)
        self.assertIsNone(self.app.errands.status())
        q = self.create()
        self.app.backend = Scripted(tool("queue.control", id=q["id"], version=q["version"], action="resume"))
        with self.assertRaises(ModelError):
            self.app.reply("Начинай", actor="minecraft:guest", owner=False)
        self.assertEqual(self.app.errands.status()["status"], "paused")

    def test_model_chosen_resume_and_pause_return_actual_queue_state(self):
        q = self.create()
        self.app.backend = Scripted(tool("queue.control", id=q["id"], version=q["version"], action="resume"),
                                    answer("Список включён, начну по наблюдениям."))
        self.app.handle("Да, начинай по этому списку")
        self.assertEqual(self.app.errands.status()["status"], "active")
        q = self.app.errands.status()
        self.app.backend = Scripted(tool("queue.control", id=q["id"], version=q["version"], action="pause"))
        result = self.app.handle("Отложим пока поручения")
        self.assertEqual(self.app.errands.status()["status"], "paused")
        self.assertEqual(result["_revision"], self.app.store.get("response_revision"))
        self.assertIn("паузе", result["reply"])

    def test_repeated_disconnect_on_a_paused_queue_does_not_change_version(self):
        self.create()
        self.app.minecraft.detach()
        version = self.app.errands.status()["version"]
        for _ in range(3):
            self.app.minecraft.detach()
        self.assertEqual(self.app.errands.status()["version"], version)

    def test_semantic_pause_needs_no_extra_follow_up_budget(self):
        q = self.create()
        self.settings.daily_requests = 1
        self.app.backend = Scripted(tool("queue.control", id=q["id"], version=q["version"], action="pause"))
        result = self.app.handle("Отложим поручения")
        self.assertIn("UnionClef подтвердил", result["reply"])
        self.assertEqual(self.app.errands.status()["status"], "paused")
        self.assertEqual(len(self.app.backend.calls), 1)

    def test_queue_text_is_not_exposed_in_shared_context_or_game_controls(self):
        marker = "PRIVATE_QUEUE_FIXTURE"
        self.create(step(note=marker), title=marker)
        for actor in ("minecraft:guest", "call:shared", "minecraft:alexplayer"):
            self.assertNotIn(marker, json.dumps(self.app.workplace.context(actor)))
        self.assertNotIn(marker, self.app.command("/queue", actor="minecraft:alexplayer"))
        self.assertIn(marker, json.dumps(self.app.workplace.context("owner")))
        self.assertNotIn(marker, json.dumps(self.app.experience.context("world:minecraft")))

    def test_queue_tools_from_game_only_log_private_notes_in_owner_scope(self):
        marker = "PRIVATE_QUEUE_FIXTURE"
        self.app.backend = Scripted(tool("queue.create", title=marker, steps=[step(note=marker)]), answer("Список сохранён"))
        self.app.reply("Составь список", actor="minecraft:alexplayer", owner=True)
        self.assertNotIn(marker, json.dumps(self.app.experience.context("world:minecraft")))
        observation = json.loads(self.app.backend.calls[-1][1][-1]["content"])["tool_observation"]
        self.assertNotIn(marker, json.dumps(observation))

    def test_queue_planner_gets_queue_and_game_but_no_private_chat_or_notes(self):
        self.app.store.remember("owner", "PRIVATE_CHAT_FIXTURE")
        self.app.store.append("owner", "user", "PRIVATE_CHAT_FIXTURE", "joker")
        self.create()
        self.resume()
        self.tick("wait")
        system, messages, kwargs = self.app.backend.calls[0]
        context = json.loads(messages[0]["content"])
        self.assertIn("inventory", context["game"])
        self.assertEqual(context["queue"]["title"], "Подготовка к стройке")
        self.assertNotIn("PRIVATE_CHAT_FIXTURE", system + json.dumps(messages))

    def test_sleep_and_wake_keep_queue_paused_and_preserve_mask_and_steps(self):
        self.create()
        self.resume()
        self.app.switch("housewife")
        self.app.runtime.sleep()
        self.app.runtime.wake()
        self.assertEqual(self.app.active, "housewife")
        self.assertEqual(self.app.errands.status()["status"], "paused")
        self.assertEqual(self.app.errands.current()["status"], "pending")
        self.assertEqual(self.entry.commands, [])

    def test_panel_controls_require_csrf_and_reject_stale_versions(self):
        self.create()
        server = make_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            q = self.app.errands.status()
            payload = json.dumps(dict(id=q["id"], version=q["version"], action="resume")).encode()
            request = urllib.request.Request(base + "/api/errands", data=payload, headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            # Read the operator page exactly as a local browser obtains the key.
            with patch.object(type(self.root), "read_text", return_value='<script>const key=__CSRF__;</script>'):
                html = urllib.request.urlopen(base).read().decode()
            key = html.split("const key=", 1)[1].split(";", 1)[0]
            request.add_header("X-Joker-Key", json.loads(key))
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response)["errands"]["status"], "active")
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 409)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
