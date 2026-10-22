"""Local ASR lifecycle and operator upload boundaries; no mic, models or network required."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import struct
import sys
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request
import wave
import unittest
from unittest.mock import Mock, patch

from joker.backends import Cancelled, ModelError
from joker.panel import make_server
from joker.stt import WhisperSTT, audio_wav, download, installed, model_dir, pcm_wav, status
from joker.stt_worker import main as worker_main, recognize
from joker.voice import AudioHTTP
from test_next_stage import Fixture


class STTFixture(Fixture):
    def setUp(self):
        super().setUp()
        self.settings.stt_backend, self.settings.stt_model = "whisper", "small"
        self.raw = pcm_wav(struct.pack("<h", 1000) * 4800)

    def model_files(self):
        files = []
        directory = self.root / "data/stt/models/small"
        directory.mkdir(parents=True, exist_ok=True)
        for name in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt", "README.md"):
            data = b"fixture-file"
            (directory / name).write_bytes(data)
            files.append(dict(name=name, url="https://huggingface.co/Systran/faster-whisper-small/resolve/revision/" + name,
                              size=len(data), sha256=hashlib.sha256(data).hexdigest()))
        self.catalog = [dict(id="small", label="Fixture small", revision="revision", files=files)]
        (self.root / "agent/shared/stt-models.json").write_text(json.dumps(self.catalog), encoding="utf-8")
        return directory

    def process(self, raw=b'{"text":"fixture transcription"}\n'):
        process = Mock()
        process.stdin, process.stdout = io.BytesIO(), io.BytesIO(raw)
        process.poll.return_value, process.wait.return_value = None, 0
        return process


class LocalSTTTests(STTFixture):
    @unittest.skipUnless(importlib.util.find_spec("faster_whisper"), "optional stt extra not installed")
    def test_installed_native_decoder_accepts_valid_pcm_without_model_download(self):
        from faster_whisper.audio import decode_audio
        samples = decode_audio(io.BytesIO(self.raw), sampling_rate=16000)
        self.assertEqual(len(samples), 1600)
        self.assertTrue(all(-1 <= sample <= 1 for sample in samples))

    def test_pcm_wav_validation_rejects_stereo_float_truncation_and_overlong_audio(self):
        canonical, seconds = audio_wav(self.raw)
        self.assertEqual(canonical, self.raw)
        self.assertEqual(seconds, .1)
        stereo = io.BytesIO()
        with wave.open(stereo, "wb") as output:
            output.setnchannels(2)
            output.setsampwidth(2)
            output.setframerate(48000)
            output.writeframes(b"\x00" * 19200)
        for raw in (b"not a wave", self.raw[:-2], pcm_wav(b"\x00\x00" * 480001), stereo.getvalue(),
                    pcm_wav(b"\x00\x00" * 4), b"x" * 1000001):
            with self.subTest(size=len(raw)), self.assertRaises(ValueError):
                audio_wav(raw)

    def test_installed_requires_complete_models_and_safe_ids(self):
        directory = self.model_files()
        self.assertEqual(installed(self.settings)[0]["id"], "small")
        (directory / "model.bin").unlink()
        self.assertEqual(installed(self.settings), [])
        for name in ("../escape", "C:/outside", "", "small.en/escape"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                model_dir(self.settings, name)

    def test_http_configuration_remains_separate_and_unknown_backend_is_not_ready(self):
        self.settings.stt_backend, self.settings.stt_model = "http", "fixture-http"
        self.settings.stt_url = "http://localhost/stt"
        self.assertTrue(status(self.settings)["ready"])
        self.settings.stt_backend = "unknown"
        self.assertFalse(status(self.settings)["ready"])
        with patch.object(AudioHTTP, "request") as http, self.assertRaises(ValueError):
            AudioHTTP(self.settings).transcribe_wav(self.raw)
        http.assert_not_called()

    def test_missing_model_never_starts_process_or_downloads(self):
        with patch("joker.stt.subprocess.Popen") as process, patch("urllib.request.urlopen") as network, \
                self.assertRaises(ModelError):
            WhisperSTT(self.settings).transcribe_wav(self.raw)
        process.assert_not_called()
        network.assert_not_called()

    def test_native_request_is_offline_and_carries_no_credentials_or_pcm_on_argv(self):
        self.model_files()
        provider, process = WhisperSTT(self.settings), self.process()
        with patch("joker.stt.importlib.util.find_spec", return_value=object()), \
                patch("joker.stt.subprocess.Popen", return_value=process) as popen, \
                patch.dict("os.environ", {"STT_API_KEY": "PRIVATE_STT_FIXTURE", "HF_TOKEN": "PRIVATE_HF_FIXTURE"}):
            self.assertEqual(provider.transcribe_wav(self.raw), "fixture transcription")
            request = json.loads(process.stdin.getvalue())
            self.assertFalse(Path(request["audio"]).exists())
            self.assertEqual(request["threads"], 4)
            env = popen.call_args.kwargs["env"]
            self.assertEqual(env["HF_HUB_OFFLINE"], "1")
            self.assertNotIn("STT_API_KEY", env)
            self.assertNotIn("HF_TOKEN", env)
            self.assertNotIn("audio", str(popen.call_args.args))
        provider.close()
        self.assertEqual(list((self.root / "data/stt/jobs").iterdir()), [])

    def test_cancelled_request_kills_worker_and_removes_temporary_audio(self):
        self.model_files()
        token, process = threading.Event(), self.process()
        process.stdin = Mock()
        process.stdin.flush.side_effect = token.set
        provider = WhisperSTT(self.settings)
        with patch("joker.stt.importlib.util.find_spec", return_value=object()), \
                patch("joker.stt.subprocess.Popen", return_value=process), self.assertRaises(Cancelled):
            provider.transcribe_wav(self.raw, token)
        process.kill.assert_called_once()
        self.assertEqual(list((self.root / "data/stt/jobs").iterdir()), [])
        with patch("joker.stt.subprocess.Popen") as popen, self.assertRaises(Cancelled):
            provider.transcribe_wav(self.raw, token)
        popen.assert_not_called()

    def test_worker_timeout_is_bounded_and_does_not_retry(self):
        self.model_files()
        now, process = [0], self.process()
        process.stdin = Mock()
        process.stdin.flush.side_effect = lambda: now.__setitem__(0, 61)
        provider = WhisperSTT(self.settings)
        with patch("joker.stt.time.monotonic", side_effect=lambda: now[0]), \
                patch("joker.stt.importlib.util.find_spec", return_value=object()), \
                patch("joker.stt.subprocess.Popen", return_value=process) as popen, self.assertRaises(ModelError):
            provider.transcribe_wav(self.raw)
        self.assertEqual(popen.call_count, 1)
        process.kill.assert_called_once()

    def test_crash_invalid_response_and_oversized_output_are_isolated(self):
        self.model_files()
        for raw in (b"", b"not-json\n", b'{"text":true}\n', b'{"error":"PRIVATE_INTERNAL_FIXTURE"}\n',
                    json.dumps({"text": "x"*1001}).encode()+b"\n", b"x"*16001+b"\n"):
            provider, process = WhisperSTT(self.settings), self.process(raw)
            with self.subTest(size=len(raw)), patch("joker.stt.importlib.util.find_spec", return_value=object()), \
                    patch("joker.stt.subprocess.Popen", return_value=process), self.assertRaises(ModelError) as error:
                provider.transcribe_wav(self.raw)
            self.assertNotIn("PRIVATE_INTERNAL_FIXTURE", str(error.exception))
            self.assertIsNone(provider.worker)
            self.assertFalse(provider.lock.locked())

    def test_download_verifies_bytes_and_does_not_replace_files_on_failure(self):
        directory = self.model_files()
        (directory / "model.bin").unlink()
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"wrong")), self.assertRaises(ValueError):
            download(self.settings, "small")
        self.assertFalse((directory / "model.bin").exists())
        self.assertEqual(list(directory.glob("*.part")), [])
        with patch("urllib.request.urlopen", return_value=io.BytesIO(b"fixture-file")):
            download(self.settings, "small")
        self.assertEqual(installed(self.settings)[0]["id"], "small")
        with patch("urllib.request.urlopen") as network:
            download(self.settings, "small")
        network.assert_not_called()

    def test_download_rejects_arbitrary_url_path_and_unknown_model(self):
        self.model_files()
        for name, url in (("../escape", "https://example.invalid/model"), ("model.bin", "http://example.invalid/model")):
            entry = self.catalog[0]
            entry["files"] = [dict(name=name, url=url, size=1, sha256="a"*64)]
            with patch("joker.stt.catalog", return_value=[entry]), patch("urllib.request.urlopen") as network, \
                    self.assertRaises(ValueError):
                download(self.settings, "small")
            network.assert_not_called()
        with self.assertRaises(ValueError):
            download(self.settings, "unknown")

    def test_worker_uses_cpu_int8_local_files_and_reuses_model(self):
        directory = self.model_files()
        audio = self.root / "fixture.wav"
        audio.write_bytes(self.raw)
        request = dict(audio=str(audio), model=str(directory), language="ru", threads=4)
        source = (json.dumps(request)+"\n")*2 + '{"audio":"PRIVATE_MALFORMED_FIXTURE"}\n'
        model, output = Mock(), io.StringIO()
        with patch.dict(sys.modules, {"faster_whisper": SimpleNamespace(WhisperModel=model)}), \
                patch("joker.stt_worker.recognize", return_value={"text": "fixture"}), \
                patch("sys.stdin", SimpleNamespace(buffer=io.BytesIO(source.encode()))), patch("sys.stdout", output):
            worker_main()
        model.assert_called_once_with(str(directory), device="cpu", compute_type="int8", cpu_threads=4,
                                      num_workers=1, local_files_only=True)
        self.assertEqual(json.loads(output.getvalue().splitlines()[-1]), {"error": "recognition_failed"})
        self.assertNotIn("PRIVATE_MALFORMED_FIXTURE", output.getvalue())

    def test_neural_vad_and_no_previous_text_are_used_without_phrase_replacements(self):
        model = Mock()
        model.transcribe.return_value = ([SimpleNamespace(text="  Джокер, привет. ")], object())
        self.assertEqual(recognize(model, self.raw, "ru"), {"text": "Джокер, привет."})
        options = model.transcribe.call_args.kwargs
        self.assertTrue(options["vad_filter"])
        self.assertFalse(options["condition_on_previous_text"])
        self.assertNotIn("initial_prompt", options)
        self.assertEqual(options["language"], "ru")
        model.transcribe.return_value = ([], object())
        self.assertEqual(recognize(model, self.raw, "auto"), {"text": ""})
        self.assertIsNone(model.transcribe.call_args.kwargs["language"])

    def test_whisper_backend_never_calls_http(self):
        provider = AudioHTTP(self.settings)
        with patch.object(provider.recognizer, "transcribe_wav", return_value="Привет") as local, \
                patch.object(provider, "request") as http:
            self.assertEqual(provider.transcribe_wav(self.raw), "Привет")
        local.assert_called_once()
        http.assert_not_called()

    def test_public_name_hint_is_given_to_model_without_replacing_transcript(self):
        model = Mock()
        model.transcribe.return_value = ([SimpleNamespace(text="Джелки, привет.")], object())
        self.assertEqual(recognize(model, self.raw, "ru", "Джокер, Кирилл")["text"], "Джелки, привет.")
        self.assertEqual(model.transcribe.call_args.kwargs["hotwords"], "Имена участников: Джокер, Кирилл.")


class STTPreviewTests(STTFixture):
    def setUp(self):
        super().setUp()
        self.model_files()
        self.ready = patch("joker.stt.importlib.util.find_spec", return_value=object())
        self.ready.start()
        self.addCleanup(self.ready.stop)
        self.app.voice.provider = Mock()
        self.app.voice.provider.transcribe_wav.return_value = "Джокер, за мной."

    def test_preview_needs_no_game_or_audio_device_and_never_dispatches_text(self):
        self.app.minecraft.detach()
        with patch("sounddevice.RawInputStream") as capture, patch("sounddevice.RawOutputStream") as output:
            result = self.app.voice.transcribe_preview(self.raw, "joker")
        self.assertEqual(result["text"], "Джокер, за мной.")
        capture.assert_not_called()
        output.assert_not_called()
        self.assertEqual(self.model.calls, 0)
        self.assertEqual(self.app.store.history("owner"), [])
        self.assertTrue(self.app.minecraft.messages.empty())
        self.assertFalse(self.app.voice.enabled)

    def test_preview_quota_reserved_before_request_and_resets_by_day(self):
        self.app.store.set("voice_budget", {"day": self.app.store.now()[:10], "used": 100})
        with self.assertRaises(ModelError):
            self.app.voice.transcribe_preview(self.raw, "joker")
        self.app.voice.provider.transcribe_wav.assert_not_called()
        self.app.store.set("voice_budget", {"day": "2000-01-01", "used": 999})
        self.app.voice.transcribe_preview(self.raw, "joker")
        self.assertEqual(self.app.store.get("voice_budget")["used"], 1)

    def test_configured_daily_limit_applies_to_preview(self):
        self.settings.stt_daily_requests = 1
        self.app.voice.transcribe_preview(self.raw, "joker")
        with self.assertRaises(ModelError):
            self.app.voice.transcribe_preview(self.raw, "joker")
        self.assertEqual(self.app.voice.provider.transcribe_wav.call_count, 1)

    def test_local_stt_does_not_drop_followup_because_of_http_cooldown(self):
        voice = self.app.voice
        voice.enabled = True
        voice.provider.transcribe.return_value = "Джокер, привет."
        for _ in range(4): voice.feed("AlexPlayer", struct.pack("<h", 1000)*4800)
        voice.flush(time.monotonic()+1)
        voice.last_stt = time.monotonic()
        self.assertTrue(voice.listen_once())
        self.assertEqual(voice.provider.transcribe.call_count, 1)

    def test_stop_and_role_change_discard_inflight_preview(self):
        for operation in (self.app.stop, lambda: self.app.switch("engineer")):
            mask = self.app.active
            def changed(*args):
                operation()
                return "Старый текст"
            self.app.voice.provider.transcribe_wav.side_effect = changed
            with self.subTest(mask=mask), self.assertRaises(Cancelled):
                self.app.voice.transcribe_preview(self.raw, mask)

    def test_stale_panel_invalid_audio_and_concurrent_preview_never_start_request(self):
        with self.assertRaises(Cancelled):
            self.app.voice.transcribe_preview(self.raw, "old-role")
        with self.assertRaises(ValueError):
            self.app.voice.transcribe_preview(b"wrong", "joker")
        self.app.voice.stt_preview_lock.acquire()
        try:
            with self.assertRaises(ModelError):
                self.app.voice.transcribe_preview(self.raw, "joker")
        finally:
            self.app.voice.stt_preview_lock.release()
        self.app.voice.provider.transcribe_wav.assert_not_called()

    def test_input_preserves_internal_quiet_chunks_without_extending_end_of_speech(self):
        voice = self.app.voice
        voice.enabled = True
        voiced, quiet = struct.pack("<h", 1000)*4800, b"\x00\x00"*4800
        for _ in range(4): voice.feed("AlexPlayer", voiced)
        last = voice.buffers[("game", "AlexPlayer")]["last"]
        voice.feed("AlexPlayer", quiet)
        self.assertEqual(voice.buffers[("game", "AlexPlayer")]["last"], last)
        voice.flush(time.monotonic()+1)
        self.assertEqual(voice.audio.get_nowait().pcm, voiced*4+quiet)

    def test_quiet_onset_is_kept_as_bounded_preroll_and_cancel_clears_it(self):
        voice = self.app.voice
        voice.enabled = True
        onset, voiced = struct.pack("<h", 100)*4800, struct.pack("<h", 1000)*4800
        voice.feed("AlexPlayer", onset)
        for _ in range(4): voice.feed("AlexPlayer", voiced)
        voice.flush(time.monotonic()+1)
        self.assertEqual(voice.audio.get_nowait().pcm, onset+voiced*4)
        self.settings.voice_guests = True
        for i in range(20):
            for _ in range(4): voice.feed("Guest"+str(i), onset)
        self.assertLessEqual(len(voice.preroll), 4)
        self.assertTrue(all(len(pcm) <= 19200 for pcm, at in voice.preroll.values()))
        voice.cancel_pending()
        self.assertFalse(voice.preroll)

    def test_name_vocabulary_tracks_role_and_contains_no_private_memory(self):
        self.app.store.remember("owner", "PRIVATE_ASR_MEMORY_FIXTURE")
        self.app.switch("engineer")
        self.app.voice.transcribe_preview(self.raw, "engineer")
        vocabulary = self.app.voice.provider.transcribe_wav.call_args.args[2]
        self.assertIn(self.app.masks["engineer"]["display_name"], vocabulary)
        self.assertIn(self.app.masks["joker"]["display_name"], vocabulary)
        self.assertNotIn("PRIVATE_ASR_MEMORY_FIXTURE", vocabulary)

    def test_preview_endpoint_bounds_raw_upload_and_requires_csrf_and_current_role(self):
        (self.root / "joker").mkdir(exist_ok=True)
        (self.root / "joker/panel.html").write_text('<script>const key=__CSRF__;</script>', encoding="utf-8")
        server = make_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            request = urllib.request.Request(base+"/api/stt/preview", data=self.raw,
                headers={"Content-Type": "audio/wav", "X-Joker-Mask": "joker"})
            with self.assertRaises(urllib.error.HTTPError) as denied:
                urllib.request.urlopen(request)
            self.assertEqual(denied.exception.code, 403)
            with urllib.request.urlopen(base) as page:
                key = json.loads(page.read().decode().split("const key=", 1)[1].split(";", 1)[0])
            request.add_header("X-Joker-Key", key)
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertEqual(json.load(response)["text"], "Джокер, за мной.")
            request.add_header("Content-Type", "application/json")
            with self.assertRaises(urllib.error.HTTPError) as invalid:
                urllib.request.urlopen(request)
            self.assertEqual(invalid.exception.code, 400)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(3)
