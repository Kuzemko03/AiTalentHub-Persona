"""Opt-in voice: bounded PCM intake -> HTTP STT -> game event -> HTTP TTS -> device.

UnionClef supplies 48 kHz mono PCM16LE. Optional call input and output must
be explicitly selected devices (two virtual cables for a group call).
"""
import io
import json
import math
import queue
import re
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave

from .backends import ModelError

RATE = 48000
MAX_PCM = RATE * 2 * 10


def wav_bytes(pcm, rate=RATE):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(pcm)
    return stream.getvalue()


def read_wav(raw):
    try:
        with wave.open(io.BytesIO(raw), "rb") as stream:
            channels, rate, width = stream.getnchannels(), stream.getframerate(), stream.getsampwidth()
            frames = stream.getnframes()
            if width != 2 or channels not in (1, 2) or not 8000 <= rate <= 48000 or not 0 < frames <= rate * 60:
                raise ValueError()
            pcm = stream.readframes(frames)
            if len(pcm) != frames * width * channels:
                raise ValueError()
            return pcm, rate, channels
    except (wave.Error, ValueError, EOFError):
        raise ModelError("Синтез должен вернуть PCM WAV, 16 бит, до 60 секунд.") from None


class AudioHTTP:
    def __init__(self, settings):
        self.settings = settings

    def request(self, url, key, body, content_type):
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1")):
            raise ModelError("Для аудио нужен HTTPS endpoint или локальный HTTP endpoint.")
        headers = {"Content-Type": content_type}
        if key:
            headers["Authorization"] = "Bearer " + key
        request = urllib.request.Request(url, data=body, headers=headers)
        # No redirects: never forward a provider key or audio to a different endpoint.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *_): return None
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
                raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise ValueError()
            return raw
        except (OSError, ValueError, urllib.error.URLError):
            raise ModelError("Аудиосервис не подтвердил запрос. Проверь настройки и соединение.") from None

    def transcribe(self, pcm):
        boundary = "joker" + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{self.settings.stt_model}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
                'Content-Type: audio/wav\r\n\r\n').encode() + wav_bytes(pcm) + f"\r\n--{boundary}--\r\n".encode()
        raw = self.request(self.settings.stt_url, self.settings.stt_key, body,
                           "multipart/form-data; boundary=" + boundary)
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get("text"), str) or len(value["text"]) > 1000:
                raise ValueError()
            return value["text"].strip()
        except (ValueError, TypeError):
            raise ModelError("Распознавание вернуло некорректный текст.") from None

    def synthesize(self, text, voice):
        raw = self.request(self.settings.tts_url, self.settings.tts_key,
            json.dumps({"model": self.settings.tts_model, "voice": voice, "input": text,
                        "response_format": "wav"}, ensure_ascii=False).encode(), "application/json")
        return read_wav(raw)


class Voice:
    def __init__(self, app, provider=None):
        self.app = app
        self.provider = provider or AudioHTTP(app.settings)
        self.enabled = False
        self.lock = threading.RLock()
        self.buffers = {}
        self.audio = queue.Queue(maxsize=4)
        self.speech = queue.Queue(maxsize=4)
        self.calls = queue.Queue(maxsize=4)
        self.input_stream = None
        self.shutdown = threading.Event()
        self.cancel = threading.Event()
        self.threads = []
        self.detail = "Голос выключен."
        self.metrics = {}
        self.voices = app.store.get("role_voices", {})
        self.last_stt = 0
        self.playing = False
        self.session = 0

    def status(self):
        with self.lock:
            return {"enabled": self.enabled, "detail": self.detail, "playing": self.playing,
                    "queued": self.speech.qsize(), "metrics": dict(self.metrics),
                    "role_voices": dict(self.voices)}

    def valid(self, revision, epoch):
        return (self.enabled and not self.shutdown.is_set() and not self.app.runtime.sleeping()
                and revision == self.app.store.get("response_revision", 0)
                and (epoch is None or epoch == self.app.minecraft.epoch and self.app.minecraft.entry is not None))

    def feed(self, player, pcm, source="game"):
        if (not self.enabled or self.shutdown.is_set() or self.playing or self.app.runtime.sleeping()
                or not re.fullmatch(r"[A-Za-z0-9_]{3,16}", player)
                or player.casefold() == self.app.settings.minecraft_bot.casefold()
                or source != "call" and not self.app.settings.voice_guests and player.casefold() != self.app.settings.minecraft_owner.casefold()
                or not 0 < len(pcm) <= 19200 or len(pcm) % 2):
            return
        now = time.monotonic()
        samples = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
        rms = math.sqrt(sum(x * x for x in samples) / len(samples))
        with self.lock:
            buf = self.buffers.get(player)
            if rms < 250:
                return  # Silence is finalized by flush, not sent to a paid service.
            if buf is None:
                if len(self.buffers) >= 4:
                    return
                buf = {"pcm": bytearray(), "at": now, "last": now,
                       "revision": self.app.store.get("response_revision", 0),
                       "epoch": None if source == "call" else self.app.minecraft.epoch}
                self.buffers[player] = buf
            if len(buf["pcm"]) + len(pcm) > MAX_PCM:
                # Drop an overlong utterance as a whole instead of executing a truncated command.
                buf["overflow"] = True
            elif not buf.get("overflow"):
                buf["pcm"].extend(pcm)
            buf["last"] = now

    def flush(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            for player, buf in list(self.buffers.items()):
                if now - buf["last"] < .65:
                    continue
                del self.buffers[player]
                if buf.get("overflow") or len(buf["pcm"]) < RATE * 2 * .3:
                    continue
                try:
                    self.audio.put_nowait((player, bytes(buf["pcm"]), buf["revision"], buf["epoch"], buf["at"]))
                except queue.Full:
                    pass

    def listen_once(self):
        self.flush()
        try:
            player, pcm, revision, epoch, received = self.audio.get_nowait()
        except queue.Empty:
            return False
        with self.app.guard:
            if not self.valid(revision, epoch):
                return True
            # Both a bounded intake and a daily quota, separate from text-model quota.
            day = self.app.store.now()[:10]
            quota = self.app.store.get("voice_budget", {"day": day, "used": 0})
            if quota["day"] != day:
                quota = {"day": day, "used": 0}
            if quota["used"] >= 100 or time.monotonic() - self.last_stt < 3:
                return True
            quota["used"] += 1
            self.app.store.set("voice_budget", quota)
            self.last_stt = time.monotonic()
        started = time.monotonic()
        session = self.session
        text = self.provider.transcribe(pcm)
        with self.app.guard:
            if session == self.session and self.valid(revision, epoch) and text:
                self.metrics = {"intake_to_stt_ms": round((started - received) * 1000),
                                "stt_ms": round((time.monotonic() - started) * 1000)}
                if epoch is None:
                    aliases = ("Джокер", self.app.masks[self.app.active]["display_name"])
                    if any(re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", text, re.I) for a in aliases):
                        try: self.calls.put_nowait((text, revision, self.session))
                        except queue.Full: pass
                else:
                    self.app.minecraft.accept({"user": player, "msg": text,
                        "parse_type": "parsed", "source": "voice"})
        return True

    def call_once(self):
        try:
            text, revision, session = self.calls.get_nowait()
        except queue.Empty:
            return False
        if session != self.session or not self.valid(revision, None):
            return True
        # An audio cable has no reliable individual speaker ID. It never has owner/tool rights.
        started = time.monotonic()
        result = self.app.reply(text, actor="call:shared", expected_revision=revision)
        if session == self.session and self.valid(revision, None):
            self.metrics["reply_ms"] = round((time.monotonic() - started) * 1000)
            self.enqueue(result["reply"], revision, None)
        return True

    def enqueue(self, text, revision, epoch):
        if not self.valid(revision, epoch):
            return
        voice = self.voices.get(self.app.active, self.app.settings.tts_voice)
        try:
            self.speech.put_nowait((text[:1000], voice, revision, epoch, time.monotonic()))
        except queue.Full:
            self.detail = "Очередь голоса заполнена. Текст остаётся в игровом чате."

    def speak_once(self):
        try:
            text, voice, revision, epoch, queued = self.speech.get_nowait()
        except queue.Empty:
            return False
        if not self.valid(revision, epoch):
            return True
        started = time.monotonic()
        session = self.session
        pcm, rate, channels = self.provider.synthesize(text, voice)
        if session != self.session or not self.valid(revision, epoch):
            return True
        import sounddevice
        device = self.app.settings.voice_device
        if device.isdecimal():
            device = int(device)
        cancel = self.cancel
        with sounddevice.RawOutputStream(samplerate=rate, channels=channels, dtype="int16", device=device) as output:
            self.playing = True
            try:
                chunk = max(2 * channels, rate // 50 * 2 * channels)
                for offset in range(0, len(pcm), chunk):
                    if session != self.session or cancel.is_set() or not self.valid(revision, epoch):
                        output.abort()
                        break
                    output.write(pcm[offset:offset + chunk])
                    if offset == 0:
                        self.metrics.update(tts_ms=round((time.monotonic() - started) * 1000),
                                            text_to_sound_ms=round((time.monotonic() - queued) * 1000))
            finally:
                self.playing = False
        return True

    def cancel_pending(self):
        self.cancel.set()
        self.cancel = threading.Event()
        with self.lock:
            self.buffers.clear()
            for pending in (self.audio, self.speech, self.calls):
                while True:
                    try: pending.get_nowait()
                    except queue.Empty: break

    def command(self, text):
        token, _, arg = text.strip().partition(" ")
        if token.casefold() not in ("/голос", "/voice"):
            return None
        if arg.casefold() in ("вкл", "on"):
            self.start()
        elif arg.casefold() in ("выкл", "off"):
            self.close()
        elif arg.startswith("тембр "):
            voice = arg.removeprefix("тембр ").strip()
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", voice):
                return "Укажи ID голоса из своего TTS-сервиса."
            self.voices[self.app.active] = voice
            self.app.store.set("role_voices", self.voices)
            return "Тембр сохранён для текущей маски."
        return self.detail

    def start(self):
        if self.enabled:
            return
        s = self.app.settings
        if not all((s.stt_url, s.stt_model, s.tts_url, s.tts_model, s.tts_voice, s.voice_device)):
            raise ValueError("Настрой STT_URL/MODEL, TTS_URL/MODEL/VOICE и VOICE_OUTPUT_DEVICE в .env.")
        import importlib.util
        if importlib.util.find_spec("sounddevice") is None:
            raise ValueError('Установи голос: pip install -e ".[voice]"')
        self.shutdown.clear()
        self.session += 1
        self.enabled = True
        self.detail = "Голос включён. Обращайся по имени; вывод на выбранное аудиоустройство."
        if s.voice_input_device:
            import sounddevice
            device = int(s.voice_input_device) if s.voice_input_device.isdecimal() else s.voice_input_device
            session = self.session
            def incoming(data, frames, timing, status):
                if session != self.session:
                    return
                if status:
                    with self.lock:
                        self.buffers.pop("CallGuest", None)
                    return
                self.feed("CallGuest", bytes(data), source="call")
            try:
                self.input_stream = sounddevice.RawInputStream(device=device, samplerate=RATE,
                    channels=1, dtype="int16", blocksize=960, callback=incoming)
                self.input_stream.start()
            except Exception:
                self.close()
                raise ValueError("Не удалось открыть VOICE_INPUT_DEVICE, 48 кГц mono PCM16.") from None
        self.threads = [threading.Thread(target=self.run, args=(fn, self.session), daemon=True, name=name)
                        for fn, name in ((self.listen_once, "joker-stt"), (self.speak_once, "joker-tts"),
                                         (self.call_once, "joker-call"))]
        for thread in self.threads:
            thread.start()

    def run(self, step, session):
        while session == self.session and not self.shutdown.is_set():
            try:
                step()
            except Exception:
                self.detail = "Ошибка аудио. Проверь сервис и устройство; текстовый чат доступен."
            self.shutdown.wait(.1)

    def close(self):
        self.session += 1
        self.enabled = False
        self.shutdown.set()
        self.cancel_pending()
        if self.input_stream:
            try:
                self.input_stream.abort()
                self.input_stream.close()
            finally:
                self.input_stream = None
        self.detail = "Голос выключен."
