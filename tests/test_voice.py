import io
import importlib.util
import json
import struct
import threading
import time
import unittest
import urllib.error
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, MagicMock, patch

from joker.backends import ModelError
from joker.voice import AudioHTTP, read_wav, wav_bytes
from test_next_stage import Fixture


class VoiceTests(Fixture):
    def setUp(self):
        super().setUp()
        self.voice = self.app.voice
        self.voice.enabled = True  # Exercise workers without opening any real audio device.
        self.voice.provider = Mock()
        self.voice.provider.transcribe.return_value = "Джокер, привет"
        self.voice.provider.synthesize.return_value = (b"\x00\x00" * 1600, 16000, 1)

    def utterance(self, player="AlexPlayer", source="game"):
        for _ in range(4): self.voice.feed(player, struct.pack("<h", 1200) * 4800, source)
        self.voice.flush(time.monotonic() + 1)

    def test_game_voice_runs_through_addressing_and_same_character(self):
        self.utterance()
        self.voice.listen_once()
        self.app.minecraft.work_once()
        self.assertEqual(self.voice.provider.transcribe.call_count, 1)
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(self.entry.chats, ["Привет из теста."])
        self.assertEqual(self.voice.speech.qsize(), 1)
        self.assertIn("reply_ms", self.voice.metrics)

    def test_disabled_silence_guests_and_self_do_not_call_stt(self):
        self.voice.feed("AlexPlayer", b"\x00\x00" * 4800)
        self.utterance("Guest")
        self.utterance("JokerBot")
        self.voice.enabled = False
        self.utterance()
        self.voice.listen_once()
        self.voice.provider.transcribe.assert_not_called()

    def test_oversized_utterance_is_dropped_whole(self):
        for _ in range(102): self.voice.feed("AlexPlayer", struct.pack("<h", 1200) * 4800)
        self.assertLessEqual(len(self.voice.buffers[("game", "AlexPlayer")]["pcm"]), 960000)
        self.voice.flush(time.monotonic() + 1)
        self.voice.listen_once()
        self.voice.provider.transcribe.assert_not_called()

    def test_speaker_buffers_and_queues_are_bounded(self):
        self.settings.voice_guests = True
        for i in range(10): self.utterance("Guest" + str(i))
        self.assertLessEqual(self.voice.audio.qsize(), 4)
        for i in range(10): self.voice.feed("Other" + str(i), struct.pack("<h", 1200) * 4800)
        self.assertLessEqual(len(self.voice.buffers), 4)

    def test_stop_or_disconnect_during_stt_cannot_enqueue_old_text(self):
        self.utterance()
        def stopped(*args):
            self.app.stop()
            return "Джокер, за мной"
        self.voice.provider.transcribe.side_effect = stopped
        self.voice.listen_once()
        self.assertTrue(self.app.minecraft.messages.empty())
        self.assertEqual(self.entry.chats, [])

    def test_daily_audio_quota_is_reserved_before_request(self):
        self.app.store.set("voice_budget", {"day": self.app.store.now()[:10], "used": 100})
        self.utterance()
        self.voice.listen_once()
        self.voice.provider.transcribe.assert_not_called()

    def test_call_without_minecraft_has_no_owner_tools_or_private_memory(self):
        self.app.store.remember("owner", "PRIVATE_CALL_FIXTURE")
        self.app.minecraft.detach()
        self.voice.provider.transcribe.return_value = "Джокер, за мной"
        self.utterance("CallGuest", "call")
        self.voice.listen_once()
        self.voice.call_once()
        self.assertEqual(self.model.calls, 1)
        self.assertEqual(self.entry.chats, [])
        self.assertNotIn("PRIVATE_CALL_FIXTURE", self.model.system + self.model.context)
        self.assertEqual(self.voice.speech.qsize(), 1)

    def test_unaddressed_call_audio_never_calls_text_model(self):
        self.voice.provider.transcribe.return_value = "Разговор между людьми"
        self.utterance("CallGuest", "call")
        self.voice.listen_once()
        self.voice.call_once()
        self.assertEqual(self.model.calls, 0)

    @unittest.skipUnless(importlib.util.find_spec("sounddevice"), "optional voice extra not installed")
    def test_switch_during_tts_discards_old_audio_before_opening_device(self):
        self.voice.enqueue("Текст", 0, self.app.minecraft.epoch)
        def switched(*args):
            self.app.switch("engineer")
            return b"\x00\x00" * 1600, 16000, 1
        self.voice.provider.synthesize.side_effect = switched
        with patch("sounddevice.RawOutputStream") as stream:
            self.voice.speak_once()
        stream.assert_not_called()

    @unittest.skipUnless(importlib.util.find_spec("sounddevice"), "optional voice extra not installed")
    def test_playback_uses_explicit_device_and_stops_on_cancel(self):
        self.settings.voice_device = "42"
        self.voice.enqueue("Текст", 0, self.app.minecraft.epoch)
        output = MagicMock()
        output.write.side_effect = lambda chunk: self.app.stop()
        context = MagicMock()
        context.__enter__.return_value = output
        with patch("sounddevice.RawOutputStream", return_value=context) as stream, \
                patch.object(self.voice.routes, "stream_args", return_value={"device": 42}):
            self.voice.speak_once()
        self.assertEqual(stream.call_args.kwargs["device"], 42)
        self.assertEqual(output.write.call_count, 1)
        output.abort.assert_called_once()
        self.assertFalse(self.voice.playing)

    def test_role_voice_is_persistent_and_does_not_change_memory(self):
        self.app.switch("engineer")
        self.app.handle("/голос тембр fixture_voice")
        self.voice.enqueue("Текст", self.app.store.get("response_revision"), self.app.minecraft.epoch)
        self.assertEqual(self.voice.speech.get_nowait()[1], "fixture_voice")
        self.assertEqual(self.app.store.get("role_voices")["engineer"], "fixture_voice")

    @unittest.skipUnless(importlib.util.find_spec("sounddevice"), "optional voice extra not installed")
    def test_capture_opens_only_explicit_input_when_started(self):
        self.voice.close()
        self.settings.stt_url, self.settings.tts_url = "http://localhost/stt", "http://localhost/tts"
        self.settings.stt_model, self.settings.tts_model = "test", "test"
        self.settings.tts_voice, self.settings.voice_device = "test", "output-cable"
        def preflight(mode):
            return dict(device="input-cable", samplerate=48000, channels=1, dtype="int16") if self.settings.voice_input_device else None
        with patch("sounddevice.RawInputStream") as capture, patch("joker.voice.threading.Thread"), \
                patch.object(self.voice.routes, "preflight", side_effect=preflight):
            self.voice.start()
            capture.assert_not_called()
            self.voice.close()
            self.settings.voice_input_device = "input-cable"
            self.voice.start()
            self.assertEqual(capture.call_args.kwargs["device"], "input-cable")
            self.assertEqual(capture.call_args.kwargs["samplerate"], 48000)
            self.voice.close()
            capture.return_value.abort.assert_called_once()

    def test_wav_contract_rejects_invalid_or_truncated_audio(self):
        pcm = b"\x00\x00" * 960
        self.assertEqual(read_wav(wav_bytes(pcm)), (pcm, 48000, 1))
        for raw in (b"not wav", wav_bytes(pcm)[:-20]):
            with self.assertRaises(ModelError): read_wav(raw)


class AudioHTTPTests(Fixture):
    def test_real_http_multipart_stt_and_pcm_wav_tts_without_external_service(self):
        received = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                received.append((self.path, dict(self.headers), body))
                result = json.dumps({"text": "Джокер, привет"}).encode() if self.path == "/stt" else wav_bytes(b"\x01\x00" * 960)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(result)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.settings.stt_url = f"http://127.0.0.1:{server.server_port}/stt"
        self.settings.tts_url = f"http://127.0.0.1:{server.server_port}/tts"
        self.settings.stt_model = "fixture_stt"
        self.settings.tts_model = "fixture_tts"
        provider = AudioHTTP(self.settings)
        try:
            self.assertEqual(provider.transcribe(b"\x00\x00" * 4800), "Джокер, привет")
            self.assertEqual(provider.synthesize("Привет", "fixture_voice")[1:], (48000, 1))
            self.assertIn(b'filename="speech.wav"', received[0][2])
            self.assertIn(b"RIFF", received[0][2])
            self.assertEqual(json.loads(received[1][2])["voice"], "fixture_voice")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_bad_endpoint_and_service_failure_do_not_expose_key_or_retry(self):
        provider = AudioHTTP(self.settings)
        for url in ("http://remote.invalid/stt", "https://user:pass@example.invalid", "https://example.invalid?key=secret"):
            with self.assertRaises(ModelError): provider.request(url, "fixture_key", b"x", "text/plain")
        opener = Mock()
        opener.open.side_effect = urllib.error.URLError("fixture_key at private endpoint")
        with patch("urllib.request.build_opener", return_value=opener), self.assertRaises(ModelError) as caught:
            provider.request("https://example.invalid/stt", "fixture_key", b"x", "text/plain")
        self.assertNotIn("fixture_key", str(caught.exception))
        self.assertEqual(opener.open.call_count, 1)


if __name__ == "__main__": unittest.main()
