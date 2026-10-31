import json
import threading
from unittest.mock import Mock, patch

from joker.backends import Cancelled, ModelError
from joker.minecraft import chat_chunks
from test_next_stage import Fixture
from test_workplace import Scripted, answer, tool


class SocialTests(Fixture):
    def nearby(self, distance=20):
        self.entry.getPlayersInfo = lambda limit: [
            {"name": "AlexPlayer", "distance": "2"},
            {"name": "ZeusThunderer", "distance": str(distance)},
        ]

    def test_named_approach_never_follows_the_owner_instead(self):
        self.nearby()
        result = self.app.minecraft.execute("approach", player="zeusthunderer")
        self.assertTrue(result["accepted"])
        self.assertEqual(self.entry.chats, [";followPlayer ZeusThunderer"])
        self.assertEqual(self.app.minecraft.task["target"], "ZeusThunderer")
        self.app.minecraft.refresh()
        self.assertEqual(self.entry.stop_calls, 0)  # Owner is near; target is not.
        self.nearby(2)
        self.app.minecraft.refresh()
        self.app.minecraft.refresh()
        self.assertEqual(self.app.minecraft.task["state"], "arrived")
        self.assertEqual(self.entry.stop_calls, 1)

    def test_missing_self_distant_or_malformed_target_has_no_fallback(self):
        for player in ("ZeusThunderer", "JokerBot", "/op Guest"):
            with self.subTest(player=player):
                try:
                    result = self.app.minecraft.execute("approach", player=player)
                except ValueError:
                    continue
                self.assertFalse(result["accepted"])
        self.nearby(200)
        self.assertFalse(self.app.minecraft.execute("approach", player="ZeusThunderer")["accepted"])
        self.assertEqual(self.entry.chats, [])
        self.assertIsNone(self.app.minecraft.task)

    def test_departed_target_stops_even_while_path_is_busy(self):
        self.nearby()
        self.app.minecraft.execute("approach", player="ZeusThunderer")
        self.entry.active = True
        self.entry.getPlayersInfo = lambda limit: [{"name": "AlexPlayer", "distance": "2"}]
        self.app.minecraft.refresh()
        self.assertEqual(self.entry.stop_calls, 1)
        self.app.minecraft.refresh()
        self.assertEqual(self.app.minecraft.task["state"], "not_arrived")

    def test_target_moving_after_stop_does_not_freeze_future_choices(self):
        self.nearby(2)
        self.app.minecraft.execute("approach", player="ZeusThunderer")
        self.app.minecraft.refresh()
        self.nearby(8)
        self.app.minecraft.refresh()
        self.assertEqual(self.app.minecraft.task["state"], "not_arrived")
        self.assertIsNone(self.app.store.get("pending_game_action"))
        self.assertTrue(self.app.minecraft.execute("approach", player="ZeusThunderer")["accepted"])

    def test_public_chat_remains_available_during_movement(self):
        self.nearby()
        self.app.minecraft.execute("approach", player="ZeusThunderer")
        task_id = self.app.minecraft.task["id"]
        self.entry.active = True
        self.assertTrue(self.app.minecraft.say("Привет! Уже иду к вам.")["accepted"])
        self.assertEqual(self.app.minecraft.task["id"], task_id)

    def test_speech_writer_never_receives_private_history_or_memories(self):
        self.app.store.remember("owner", "PRIVATE_SPEECH_MEMORY")
        self.app.store.append("owner", "user", "PRIVATE_SPEECH_HISTORY", "joker")
        request = "Расскажи всем как менять трубу на кухне"
        model = self.app.backend = Scripted(tool("game.say", request=request),
            {"message": "Сначала перекрой воду и сбрось давление. Потом разбирай соединение."},
            answer("Реплику передал в игровой чат."))
        self.app.handle(request)
        public_prompt = model.calls[1][0] + json.dumps(model.calls[1][1])
        self.assertNotIn("PRIVATE_SPEECH_MEMORY", public_prompt)
        self.assertNotIn("PRIVATE_SPEECH_HISTORY", public_prompt)
        self.assertEqual(len(self.entry.chats), 1)
        self.assertTrue(self.entry.chats[0].startswith("Сначала перекрой"))

    def test_delegated_wording_accepts_a_model_composed_social_brief(self):
        self.app.backend = Scripted(tool("game.say", request="Сам придумай, как познакомиться с Ладой"),
            {"message": "Лада, привет! Что здесь строишь?"}, answer("Заговорил с Ладой."))
        self.app.handle("Ну сам придумай, что ей сказать")
        self.assertEqual(self.entry.chats, ["Лада, привет! Что здесь строишь?"])

    def test_stop_during_public_composition_prevents_sending(self):
        request = "Поздоровайся с игроками"
        def interrupted(*args, **kwargs):
            self.app.stop()
            return {"message": "Привет всем!"}
        self.app.backend = Scripted(tool("game.say", request=request), interrupted)
        with self.assertRaises(Cancelled):
            self.app.handle(request)
        self.assertEqual(self.entry.chats, [])

    def test_guests_cannot_initiate_public_posts_or_movement(self):
        for decision in (tool("game.say", request="Поздоровайся"), tool("game.approach", player="AlexPlayer"),
                         tool("life.start", goal="Гуляй и общайся")):
            self.app.backend = Scripted(decision)
            with self.assertRaises(ModelError):
                self.app.reply("Поздоровайся", actor="minecraft:guest", owner=False)
        self.assertEqual(self.entry.chats, [])

    def test_route_release_then_next_action_preserves_dialogue_and_autonomy(self):
        self.nearby()
        self.app.minecraft.execute("follow")
        self.app.runtime.state.update(enabled=True, goal="Исследуй мир")
        revision = self.app.store.get("response_revision", 0)
        self.app.backend = Scripted(tool("game.release"), tool("game.approach", player="ZeusThunderer"),
                                    answer("Иду к Зевсу."))
        result = self.app.handle("Смени маршрут и подойди к Зевсу")
        self.assertEqual(result["reply"], "Иду к Зевсу.")
        self.assertTrue(self.app.runtime.state["enabled"])
        self.assertEqual(self.app.store.get("response_revision", 0), revision)
        self.assertEqual(self.entry.chats[-1], ";followPlayer ZeusThunderer")

    def test_release_cannot_clear_uncertain_delivery(self):
        self.app.minecraft.task = {"kind": "deliver", "state": "unconfirmed"}
        self.assertFalse(self.app.minecraft.release_task()["accepted"])
        self.assertEqual(self.app.minecraft.task["state"], "unconfirmed")
        self.assertEqual(self.entry.stop_calls, 0)

    def test_ordinary_life_request_starts_goal_instead_of_answer_stopped(self):
        self.app.backend = Scripted(tool("life.start", goal="Гуляй по миру и знакомься с игроками"))
        result = self.app.handle("Погуляй по серверу и пообщайся с ребятами")
        self.assertTrue(self.app.runtime.state["enabled"])
        self.assertEqual(self.app.runtime.state["goal"], "Гуляй по миру и знакомься с игроками")
        self.assertNotIn("Ответ остановлен", result["reply"])
        self.assertTrue(result["_transition"])

    def test_chat_splits_on_words_without_truncating_the_sentence(self):
        text = "Сначала перекрой воду, потом сбрось давление.\n" * 10
        parts = chat_chunks(text)
        self.assertGreater(len(parts), 1)
        self.assertEqual(" ".join(parts), " ".join(text.split()))
        self.assertTrue(all(len(p.encode("utf-16-le")) // 2 <= 240 for p in parts))
        self.assertTrue(all(not p.startswith(("/", "@", ";", "!", "$", "\\")) for p in chat_chunks('/op ' + 'слово ' * 70)))

    def test_game_reply_sends_and_voices_the_full_text(self):
        text = "Сначала перекрой воду, потом сбрось давление. " * 10
        self.app.backend = Scripted(answer(text))
        self.app.voice.enqueue = Mock()
        self.app.minecraft.accept({"user": "AlexPlayer", "msg": "JokerBot, расскажи", "parse_type": "parsed"})
        with patch.object(self.app.minecraft.shutdown, "wait", return_value=False):
            self.app.minecraft.work_once()
        self.assertEqual(" ".join(self.entry.chats), " ".join(text.split()))
        self.assertEqual(self.app.voice.enqueue.call_args.args[0], " ".join(text.split()))

    def test_stop_after_first_chat_part_prevents_remaining_parts(self):
        text = "Сначала перекрой воду, потом сбрось давление. " * 10
        self.app.backend = Scripted(answer(text))
        send = self.entry.ChatMessage
        def interrupted(part):
            send(part)
            self.app.stop()
        self.entry.ChatMessage = interrupted
        self.app.minecraft.accept({"user": "AlexPlayer", "msg": "JokerBot, расскажи", "parse_type": "parsed"})
        with patch.object(self.app.minecraft.shutdown, "wait", return_value=False):
            self.app.minecraft.work_once()
        self.assertEqual(len(self.entry.chats), 1)

    def test_chat_rejects_commands_and_has_durable_no_replay(self):
        for text in ("/op Guest", "@get diamond 2", ";followPlayer Guest", "!hello", "😀" * 121):
            with self.subTest(text=text), self.assertRaises(ValueError):
                self.app.minecraft.say(text)
        self.assertTrue(self.app.minecraft.say("Привет всем!")["accepted"])
        self.assertFalse(self.app.minecraft.say("Привет всем!")["accepted"])
        self.assertFalse(self.app.minecraft.say("А кто тут строит?")["accepted"])
        self.assertEqual(self.entry.chats, ["Привет всем!"])

    def test_uncertain_chat_is_not_retried(self):
        self.entry.ChatMessage = Mock(side_effect=OSError("connection lost"))
        self.assertIsNone(self.app.minecraft.say("Привет всем!")["accepted"])
        self.assertFalse(self.app.minecraft.say("Привет всем!")["accepted"])
        self.entry.ChatMessage.assert_called_once()

    def test_autonomy_can_choose_visible_player_and_public_speech(self):
        self.nearby()
        self.app.store.remember("owner", "PRIVATE_SOCIAL_RUNTIME")
        self.app.handle("/цель Познакомься с игроками рядом")
        self.app.handle("/автономность вкл")
        self.model.decision.update(action="approach", player="ZeusThunderer")
        self.app.runtime.tick()
        self.assertEqual(self.entry.chats, [";followPlayer ZeusThunderer"])
        self.assertNotIn("PRIVATE_SOCIAL_RUNTIME", self.model.context)
        self.assertIn("ZeusThunderer", self.model.context)
        self.app.minecraft.stop_task()
        self.app.runtime.next_tick = 0
        self.model.decision.update(action="say", message="Зевс, что здесь строите?")
        self.app.runtime.tick()
        self.assertEqual(self.entry.chats[-1], "Зевс, что здесь строите?")

    def test_uncertain_autonomous_chat_pauses_further_actions(self):
        self.app.handle("/цель Познакомься с игроками")
        self.app.handle("/автономность вкл")
        self.model.decision.update(action="say", message="Привет всем!")
        self.entry.ChatMessage = Mock(side_effect=OSError("connection lost"))
        self.app.runtime.tick()
        self.assertFalse(self.app.runtime.state["enabled"])
        self.app.runtime.next_tick = 0
        self.app.runtime.tick()
        self.entry.ChatMessage.assert_called_once()

    def test_known_queued_chat_does_not_disable_autonomy_on_restart(self):
        from joker.runtime import Runtime
        self.app.handle("/цель Познакомься с игроками")
        self.app.handle("/автономность вкл")
        self.model.decision.update(action="say", message="Привет всем!")
        self.app.runtime.tick()
        self.assertNotIn("pending_action", self.app.runtime.state)
        restored = Runtime(self.app)
        self.assertTrue(restored.state["enabled"])
        restored.tick()
        self.assertEqual(self.entry.chats, ["Привет всем!"])
