"""Local voice lifecycle, native worker isolation and explicit previews; no live device."""
import base64
import hashlib
import io
import json
import queue
import shutil
import sys
import threading
import urllib.error
import urllib.request
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from joker.backends import Cancelled, ModelError
from joker.panel import make_server
from joker.tts import PiperTTS, download, installed, pcm_wav, speech_text, voice_files
from joker.tts_worker import main as worker_main, native_path, synthesize as worker_synthesize
from joker.voice import AudioHTTP, read_wav
from test_next_stage import Fixture


class TTSFixture(Fixture):
    def model_file(self, voice="fixture-ru"):
        directory = self.root / "data/voices"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (voice + ".onnx")).write_bytes(b"fixture-not-a-real-neural-model")
        (directory / (voice + ".onnx.json")).write_text(json.dumps({"audio": {"sample_rate": 22050},
            "num_speakers": 1}), encoding="utf-8")
        return directory

    def response(self, **changes):
        result = {"pcm": base64.b64encode(b"\x01\x00" * 2205).decode(), "rate": 22050, "channels": 1}
        result.update(changes)
        return json.dumps(result).encode() + b"\n"

    def process(self, raw):
        process = Mock()
        process.stdout = io.BytesIO(raw)
        process.stdin = io.BytesIO()
        process.poll.return_value = None
        process.wait.return_value = 0
        return process


class LocalTTSTests(TTSFixture):
    def test_installed_models_require_config_valid_format_and_local_ids(self):
        directory = self.model_file()
        self.assertEqual(installed(self.settings)[0]["id"], "fixture-ru")
        for name in ("../fixture", "C:/outside", "fixture.onnx", ""):
            with self.subTest(name=name), self.assertRaises(ValueError):
                voice_files(self.settings, name)
        (directory / "fixture-ru.onnx.json").write_text('{"audio":{"sample_rate":true}}', encoding="utf-8")
        self.assertEqual(installed(self.settings), [])

    def test_missing_model_never_starts_a_process_or_download(self):
        provider = PiperTTS(self.settings)
        with patch("joker.tts.subprocess.Popen") as process, patch("urllib.request.urlopen") as network:
            with self.assertRaises(ModelError):
                provider.synthesize("Привет", "missing")
        process.assert_not_called()
        network.assert_not_called()

    def test_native_worker_receives_text_on_pipe_without_provider_credentials(self):
        self.model_file()
        process = self.process(self.response())
        provider = PiperTTS(self.settings)
        with patch("joker.tts.importlib.util.find_spec", return_value=object()), \
                patch("joker.tts.subprocess.Popen", return_value=process) as popen, \
                patch.dict("os.environ", {"TTS_API_KEY": "PRIVATE_TTS_FIXTURE", "HF_TOKEN": "PRIVATE_HF_FIXTURE"}):
            self.assertEqual(provider.synthesize("Привет", "fixture-ru")[1:], (22050, 1))
            request = json.loads(process.stdin.getvalue())
            self.assertEqual(request["text"], "Привет")
            self.assertNotIn("Привет", str(popen.call_args.args))
            self.assertNotIn("TTS_API_KEY", popen.call_args.kwargs["env"])
            self.assertNotIn("HF_TOKEN", popen.call_args.kwargs["env"])
            self.assertNotIn("shell", popen.call_args.kwargs)
        provider.close()
        self.assertTrue(process.stdin.closed)

    def test_crashed_or_malformed_worker_does_not_crash_application(self):
        self.model_file()
        for raw in (b"", b"not-json\n", self.response(rate=True), self.response(pcm="INVALID!"), b'{"error":"fixture"}\n'):
            process = self.process(raw)
            provider = PiperTTS(self.settings)
            with self.subTest(raw=raw[:20]), patch("joker.tts.importlib.util.find_spec", return_value=object()), \
                    patch("joker.tts.subprocess.Popen", return_value=process), self.assertRaises(ModelError):
                provider.synthesize("Привет", "fixture-ru")
            self.assertIsNone(provider.worker)
            process.kill.assert_called_once()
        self.assertIsNotNone(self.app.status())

    def test_cancellation_kills_worker_and_pre_cancelled_request_does_not_start_it(self):
        self.model_file()
        cancel = threading.Event()
        process = self.process(self.response())
        process.stdin = Mock()
        process.stdin.flush.side_effect = cancel.set
        provider = PiperTTS(self.settings)
        with patch("joker.tts.importlib.util.find_spec", return_value=object()), \
                patch("joker.tts.subprocess.Popen", return_value=process), self.assertRaises(Cancelled):
            provider.synthesize("Привет", "fixture-ru", cancel)
        process.kill.assert_called_once()
        with patch("joker.tts.subprocess.Popen") as popen, self.assertRaises(Cancelled):
            provider.synthesize("Привет", "fixture-ru", cancel)
        popen.assert_not_called()

    def test_oversized_response_is_bounded_and_worker_is_closed(self):
        self.model_file()
        provider = PiperTTS(self.settings)
        provider.MAX_RESPONSE = 5
        process = self.process(b"123456789\n")
        with patch("joker.tts.importlib.util.find_spec", return_value=object()), \
                patch("joker.tts.subprocess.Popen", return_value=process), self.assertRaises(ModelError):
            provider.synthesize("Привет", "fixture-ru")
        process.kill.assert_called_once()

    def test_native_timeout_kills_the_worker(self):
        self.model_file()
        now = [0]
        process = self.process(self.response())
        process.stdin = Mock()
        process.stdin.flush.side_effect = lambda: now.__setitem__(0, 61)
        provider = PiperTTS(self.settings)
        with patch("joker.tts.time.monotonic", side_effect=lambda: now[0]), \
                patch("joker.tts.importlib.util.find_spec", return_value=object()), \
                patch("joker.tts.subprocess.Popen", return_value=process), self.assertRaises(ModelError):
            provider.synthesize("Привет", "fixture-ru")
        process.kill.assert_called_once()

    def test_worker_bounds_duration_and_format(self):
        good = SimpleNamespace(sample_rate=22050, sample_channels=1, sample_width=2,
                               audio_int16_bytes=b"\x01\x00" * 2205)
        voice = Mock()
        voice.synthesize.return_value = [good]
        self.assertEqual(worker_synthesize(voice, "Привет")["rate"], 22050)
        for chunk in (SimpleNamespace(**{**vars(good), "sample_width": 4}),
                      SimpleNamespace(**{**vars(good), "audio_int16_bytes": b"x"}),
                      SimpleNamespace(**{**vars(good), "audio_int16_bytes": b"\x00\x00" * (22050 * 61)})):
            voice.synthesize.return_value = [chunk]
            with self.assertRaises(ValueError):
                worker_synthesize(voice, "Привет")

    def test_worker_reuses_one_model_and_does_not_put_text_in_error(self):
        self.model_file()
        model, config = voice_files(self.settings, "fixture-ru")
        request = dict(model=str(model), config=str(config), text="Привет")
        data = (json.dumps(request) + "\n") * 2 + '{"text":"PRIVATE_BAD_REQUEST_FIXTURE"}\n'
        stdin, stdout = SimpleNamespace(buffer=io.BytesIO(data.encode())), io.StringIO()
        voice = Mock()
        fake = SimpleNamespace(__file__=str(self.root / "piper/__init__.py"), PiperVoice=voice)
        with patch.dict(sys.modules, {"piper": fake}), patch("joker.tts_worker.native_path", return_value="ascii-path"), \
                patch("joker.tts_worker.synthesize", return_value={"rate": 22050}), \
                patch("sys.stdin", stdin), patch("sys.stdout", stdout):
            worker_main()
        voice.load.assert_called_once()
        results = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual(results[-1], {"error": "synthesis_failed"})
        self.assertNotIn("PRIVATE_BAD_REQUEST_FIXTURE", stdout.getvalue())

    def test_windows_native_path_uses_ascii_alias_or_fails_before_native_init(self):
        def alias(path, buffer, length):
            buffer.value = "C:/ASCII/ESPEAK~1"
            return len(buffer.value)
        with patch("joker.tts_worker.os.name", "nt"), \
                patch("joker.tts_worker.ctypes.windll", SimpleNamespace(kernel32=SimpleNamespace(GetShortPathNameW=alias)), create=True):
            self.assertEqual(native_path("C:/Персонаж/espeak-ng-data"), "C:/ASCII/ESPEAK~1")
        with patch("joker.tts_worker.os.name", "nt"), \
                patch("joker.tts_worker.ctypes.windll", SimpleNamespace(kernel32=SimpleNamespace(GetShortPathNameW=Mock(return_value=0))), create=True):
            with self.assertRaises(ValueError):
                native_path("C:/Персонаж/espeak-ng-data")

    def test_download_verifies_digest_and_keeps_partial_files_out_of_catalog(self):
        data = b"public-model-fixture"
        spec = {"name": "fixture-ru.onnx", "url": "https://huggingface.co/rhasspy/piper-voices/resolve/revision/file",
                "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        catalog = [{"id": "fixture-ru", "revision": "revision", "files": [spec]}]
        with patch("joker.tts.catalog", return_value=catalog), patch("urllib.request.urlopen", return_value=io.BytesIO(b"broken")):
            with self.assertRaises(ValueError):
                download(self.settings, "fixture-ru")
        self.assertEqual(list((self.root / "data/voices").iterdir()), [])
        with patch("joker.tts.catalog", return_value=catalog), patch("urllib.request.urlopen", return_value=io.BytesIO(data)):
            download(self.settings, "fixture-ru")
        with patch("joker.tts.catalog", return_value=catalog), patch("urllib.request.urlopen") as network:
            download(self.settings, "fixture-ru")
        network.assert_not_called()

    def test_download_rejects_arbitrary_url_or_path(self):
        for name, url in (("../escape.onnx", "https://example.invalid/voice"), ("fixture-ru.onnx", "http://example.invalid/voice")):
            entry = {"id": "fixture-ru", "revision": "revision", "files": [dict(name=name, url=url, size=1, sha256="a"*64)]}
            with patch("joker.tts.catalog", return_value=[entry]), patch("urllib.request.urlopen") as network, self.assertRaises(ValueError):
                download(self.settings, "fixture-ru")
            network.assert_not_called()


class SpeechTests(TTSFixture):
    def setUp(self):
        super().setUp()
        self.voice = self.app.voice
        self.voice.provider = Mock()
        self.voice.provider.synthesize.return_value = (b"\x01\x00" * 2205, 22050, 1)
        self.settings.tts_voice = "fixture-voice"

    def test_tts_mode_requires_no_stt_and_never_opens_audio_input(self):
        self.settings.tts_url, self.settings.tts_model, self.settings.voice_device = "http://localhost/tts", "fixture", "cable"
        self.settings.voice_input_device = "must-not-be-opened"
        with patch("sounddevice.RawInputStream") as capture, patch("joker.voice.threading.Thread") as thread:
            self.voice.start(mode="tts")
        capture.assert_not_called()
        self.assertEqual(thread.call_args.kwargs["name"], "joker-tts")
        self.assertEqual(thread.call_count, 1)
        self.assertEqual(self.app.store.get("voice_mode"), "tts")
        self.voice.feed("AlexPlayer", b"\x01\x00" * 960)
        self.assertFalse(self.voice.listen_once())
        self.voice.provider.transcribe.assert_not_called()

    def test_text_chat_answers_are_enqueued_for_speech(self):
        self.voice.enabled, self.voice.mode = True, "tts"
        self.app.minecraft.accept({"user": "AlexPlayer", "msg": "Джокер, привет", "parse_type": "parsed"})
        self.app.minecraft.work_once()
        self.assertEqual(self.voice.speech.qsize(), 1)
        self.voice.provider.transcribe.assert_not_called()

    def test_preview_needs_no_device_or_mic_and_only_receives_explicit_text(self):
        self.app.store.remember("owner", "PRIVATE_TTS_MEMORY_FIXTURE")
        with patch("sounddevice.RawOutputStream") as output, patch("sounddevice.RawInputStream") as capture:
            raw = self.voice.preview("**Привет!** [Дом](https://example.invalid)", mask="joker")
        self.assertEqual(read_wav(raw)[1:], (22050, 1))
        self.assertEqual(self.voice.provider.synthesize.call_args.args[0], "Привет! Дом")
        output.assert_not_called()
        capture.assert_not_called()
        self.assertFalse(self.voice.enabled)
        self.assertEqual(self.model.calls, 0)

    def test_concurrent_preview_is_rejected_without_another_request(self):
        self.voice.preview_lock.acquire()
        try:
            with self.assertRaises(ModelError):
                self.voice.preview("Текст")
        finally:
            self.voice.preview_lock.release()
        self.voice.provider.synthesize.assert_not_called()

    def test_tts_quota_reserved_before_request_and_resets_by_utc_day(self):
        self.settings.tts_daily_characters = 5
        self.voice.preview("12345")
        self.assertEqual(self.app.store.get("tts_budget")["used"], 5)
        with self.assertRaises(ModelError):
            self.voice.preview("1")
        self.assertEqual(self.voice.provider.synthesize.call_count, 1)
        self.app.store.set("tts_budget", {"day": "2000-01-01", "used": 999})
        self.voice.preview("1")
        self.assertEqual(self.app.store.get("tts_budget")["used"], 1)

    def test_stop_switch_and_timbre_change_cancel_inflight_preview(self):
        for operation in (self.app.stop, lambda: self.app.switch("engineer"), lambda: self.voice.set_voice("another-voice")):
            def stopped(*args):
                operation()
                return b"\x01\x00" * 2205, 22050, 1
            self.voice.provider.synthesize.side_effect = stopped
            with self.subTest(operation=operation), self.assertRaises(Cancelled):
                self.voice.preview("Привет")

    def test_role_voice_changes_preserve_game_and_memory_and_reject_stale_panel(self):
        self.app.store.remember("owner", "Построить дом")
        self.app.minecraft.execute("follow")
        self.voice.set_voice("first-voice", mask="joker")
        self.app.switch("engineer")
        with self.assertRaises(Cancelled):
            self.voice.set_voice("wrong-voice", mask="joker")
        self.voice.set_voice("second-voice", mask="engineer")
        self.assertEqual(self.voice.selected_voice(), "second-voice")
        self.assertEqual(self.app.minecraft.task["kind"], "follow")
        self.assertIn("Построить дом", self.app.store.facts("owner"))
        self.app.switch("joker")
        self.assertEqual(self.voice.selected_voice(), "first-voice")

    def test_long_reply_is_not_silently_truncated_and_stale_preview_is_rejected(self):
        self.voice.enabled = True
        self.voice.enqueue("x" * 1001, 0, self.app.minecraft.epoch)
        self.assertTrue(self.voice.speech.empty())
        with self.assertRaises(Cancelled):
            self.voice.preview("Текст", mask="old-persona")
        self.voice.provider.synthesize.assert_not_called()

    def test_local_backend_does_not_send_http_or_key(self):
        self.settings.tts_backend = "piper"
        self.settings.tts_key = "PRIVATE_API_FIXTURE"
        provider = AudioHTTP(self.settings)
        provider.local.synthesize = Mock(return_value=(b"\x01\x00" * 10, 22050, 1))
        token = threading.Event()
        with patch.object(provider, "request") as http:
            provider.synthesize("Привет", "fixture-ru", token)
        http.assert_not_called()
        self.assertEqual(provider.local.synthesize.call_args.args, ("Привет", "fixture-ru", token))

    def test_audio_http_cancelled_before_request_never_sends_text(self):
        token = threading.Event()
        token.set()
        provider = AudioHTTP(self.settings)
        with patch.object(provider, "request") as http, self.assertRaises(Cancelled):
            provider.synthesize("Текст", "voice", token)
        http.assert_not_called()

    def test_preview_endpoint_is_csrf_protected_and_returns_bounded_wav(self):
        (self.root / "joker").mkdir(exist_ok=True)
        (self.root / "joker/panel.html").write_text('<script>const key=__CSRF__;</script>', encoding="utf-8")
        server = make_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            request = urllib.request.Request(base + "/api/voice/preview", data=json.dumps(
                {"text": "Привет", "mask": "joker"}).encode(), headers={"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(request)
            self.assertEqual(caught.exception.code, 403)
            self.voice.provider.synthesize.assert_not_called()
            with urllib.request.urlopen(base) as page:
                self.assertIn("media-src 'self' blob:", page.headers["Content-Security-Policy"])
                key = json.loads(page.read().decode().split("const key=", 1)[1].split(";", 1)[0])
            request.add_header("X-Joker-Key", key)
            with urllib.request.urlopen(request) as audio:
                self.assertEqual(audio.headers["Content-Type"], "audio/wav")
                self.assertEqual(audio.headers["Cache-Control"], "no-store")
                self.assertEqual(read_wav(audio.read())[1:], (22050, 1))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
