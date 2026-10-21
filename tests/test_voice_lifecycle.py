"""Regression checks for cancellation between queue claim and provider calls."""
import struct
import threading
import time
from unittest.mock import Mock, patch

from joker.backends import Cancelled, ModelError
from test_next_stage import Fixture


class VoiceLifecycleTests(Fixture):
    def setUp(self):
        super().setUp()
        self.voice = self.app.voice
        self.voice.enabled = True
        self.voice.provider = Mock()
        self.voice.provider.synthesize.return_value = (b"\x00\x00" * 1600, 16000, 1)
        self.voice.provider.transcribe.return_value = "Джокер, привет"

    def utterance(self, player="AlexPlayer", source="game"):
        for _ in range(4):
            self.voice.feed(player, struct.pack("<h", 1200) * 4800, source)
        self.voice.flush(time.monotonic() + 1)

    def restart(self):
        self.voice.close()
        self.voice.enabled = True
        self.voice.shutdown.clear()
        self.voice.session += 1

    def test_timbre_change_after_speech_claim_cannot_synthesize_old_voice(self):
        self.voice.enqueue("Старая реплика", 0, self.app.minecraft.epoch)
        take = self.voice.speech.get_nowait
        def changed():
            job = take()
            self.voice.set_voice("new-voice")
            return job
        with patch.object(self.voice.speech, "get_nowait", side_effect=changed), \
                patch("sounddevice.RawOutputStream") as output:
            self.voice.speak_once()
        self.voice.provider.synthesize.assert_not_called()
        output.assert_not_called()

    def test_restart_after_audio_claim_cannot_transcribe_previous_session(self):
        self.utterance()
        take = self.voice.audio.get_nowait
        def restarted():
            job = take()
            self.restart()
            return job
        with patch.object(self.voice.audio, "get_nowait", side_effect=restarted):
            self.voice.listen_once()
        self.assertEqual(self.voice.provider.transcribe.call_count, 0)
        self.assertTrue(self.app.minecraft.messages.empty())

    def test_timbre_change_during_stt_discards_the_recognized_old_request(self):
        self.utterance()
        def changed(*args):
            self.voice.set_voice("new-voice")
            return "Джокер, за мной"
        self.voice.provider.transcribe.side_effect = changed
        self.voice.listen_once()
        self.assertTrue(self.app.minecraft.messages.empty())

    def test_game_and_call_audio_with_same_name_have_separate_buffers(self):
        self.settings.voice_guests = True
        pcm = struct.pack("<h", 1200) * 4800
        for _ in range(4):
            self.voice.feed("CallGuest", pcm, "game")
            self.voice.feed("CallGuest", pcm, "call")
        self.voice.flush(time.monotonic() + 1)
        self.assertEqual(self.voice.audio.qsize(), 2)

    def test_cancel_while_waiting_for_budget_lock_does_not_spend_quota(self):
        token = self.voice.cancel
        guard = Mock()
        guard.__enter__ = Mock(side_effect=token.set)
        guard.__exit__ = Mock(return_value=False)
        with patch.object(self.app, "guard", guard), self.assertRaises(Cancelled):
            self.voice.render("Текст", "voice", token)
        self.voice.provider.synthesize.assert_not_called()
        self.assertIsNone(self.app.store.get("tts_budget"))

    def test_recognized_game_command_cannot_run_after_voice_restart(self):
        self.utterance()
        self.voice.listen_once()
        self.assertEqual(self.app.minecraft.messages.qsize(), 1)
        self.restart()
        self.app.minecraft.work_once()
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(self.entry.chats, [])

    def test_voice_off_during_model_decision_prevents_game_tool(self):
        self.utterance()
        self.voice.listen_once()
        def decide(system, messages, cancel, **kwargs):
            self.voice.close()
            self.assertTrue(cancel.is_set())
            from test_workplace import tool
            return tool("game.follow")
        with patch.object(self.model, "generate", side_effect=decide), \
                patch.object(self.app.minecraft, "execute") as execute:
            self.app.minecraft.work_once()
        execute.assert_not_called()
        self.assertEqual(self.entry.chats, [])
        self.assertTrue(self.voice.speech.empty())

    def test_call_reply_from_previous_voice_session_is_not_spoken(self):
        self.utterance("CallGuest", "call")
        self.voice.listen_once()
        def reply(*args, **kwargs):
            self.restart()
            return {"reply": "Старый ответ"}
        with patch.object(self.app, "reply", side_effect=reply) as generated:
            self.voice.call_once()
        self.assertTrue(generated.call_args.kwargs["cancel_event"].is_set())
        self.assertTrue(self.voice.speech.empty())

    def test_old_worker_does_not_consume_new_session_queue(self):
        old = self.voice.session
        self.restart()
        self.voice.enqueue("Новая реплика", 0, self.app.minecraft.epoch)
        self.assertFalse(self.voice.speak_once(expected_session=old))
        self.assertEqual(self.voice.speech.qsize(), 1)
        self.utterance()
        self.assertFalse(self.voice.listen_once(expected_session=old))
        self.assertEqual(self.voice.audio.qsize(), 1)

    def test_old_call_callback_cannot_feed_new_session(self):
        old = self.voice.session
        self.restart()
        self.voice.feed("CallGuest", struct.pack("<h", 1200) * 4800, "call", expected_session=old)
        self.assertFalse(self.voice.buffers)

    def test_voice_cancellation_before_generation_does_not_write_or_spend(self):
        token = self.voice.cancel
        token.set()
        with self.assertRaises(Cancelled):
            self.app.reply("Старый запрос", cancel_event=token)
        self.assertEqual(self.app.store.history("owner"), [])
        self.assertEqual(self.model.calls, 0)

    def test_old_wake_command_cannot_resume_after_voice_restart(self):
        ticket = self.voice.ticket(0, self.app.minecraft.epoch)
        strip = self.app.minecraft.strip_address
        def restarted(text):
            self.restart()
            return strip(text)
        with patch.object(self.app.minecraft, "strip_address", side_effect=restarted), \
                patch.object(self.app.runtime, "wake") as wake:
            self.app.minecraft.accept({"user": "AlexPlayer", "msg": "Джокер, /wake"}, voice_ticket=ticket)
        wake.assert_not_called()

    def test_owner_role_transition_acknowledges_with_new_voice_ticket(self):
        from test_workplace import tool
        self.utterance()
        self.voice.listen_once()
        old_cancel = self.voice.cancel
        with patch.object(self.model, "generate", return_value=tool("role.select", id="engineer")):
            self.app.minecraft.work_once()
        self.assertEqual(self.app.active, "engineer")
        self.assertTrue(old_cancel.is_set())
        self.assertEqual(len(self.entry.chats), 1)
        job = self.voice.speech.get_nowait()
        self.assertIs(job.ticket.cancel, self.voice.cancel)
        self.assertTrue(self.voice.current(job.ticket))

    def test_cancel_while_waiting_for_previous_output_never_opens_device(self):
        self.voice.enqueue("Реплика", 0, self.app.minecraft.epoch)
        self.voice.playback_lock.acquire()
        thread = threading.Thread(target=self.voice.speak_once)
        with patch("sounddevice.RawOutputStream") as stream:
            thread.start()
            # The waiter can be cancelled before the prior device has finished closing.
            self.voice.close()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            stream.assert_not_called()
        self.voice.playback_lock.release()

    def test_driver_abort_error_still_closes_input_and_leaves_voice_disabled(self):
        stream = Mock()
        stream.abort.side_effect = RuntimeError("fixture-disconnected")
        self.voice.input_stream = stream
        self.voice.provider.close.side_effect = RuntimeError("fixture-worker")
        self.voice.close()
        stream.close.assert_called_once()
        self.assertIsNone(self.voice.input_stream)
        self.assertFalse(self.voice.enabled)
        self.assertTrue(self.voice.shutdown.is_set())

    def test_current_worker_surfaces_actionable_provider_error(self):
        step = Mock(side_effect=ModelError("Устройство не поддерживает формат."))
        with patch.object(self.voice.shutdown, "wait", side_effect=lambda _: self.voice.shutdown.set()):
            self.voice.run(step, self.voice.session)
        self.assertEqual(self.voice.detail, "Устройство не поддерживает формат.")

    def test_timbre_change_during_failed_provider_request_drops_stale_error(self):
        self.utterance()
        def failed(*args):
            self.voice.set_voice("new-voice")
            raise ModelError("Сбой прежнего запроса.")
        self.voice.provider.transcribe.side_effect = failed
        self.assertTrue(self.voice.listen_once())
        self.assertTrue(self.app.minecraft.messages.empty())
