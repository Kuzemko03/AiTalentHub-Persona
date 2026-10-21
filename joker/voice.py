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
from typing import NamedTuple

from .backends import Cancelled, ModelError
from .audio_routes import AudioRoutes
from .tts import MAX_TEXT, PiperTTS, installed, pcm_wav, speech_text

RATE = 48000
MAX_PCM = RATE * 2 * 10


class VoiceTicket(NamedTuple):
    revision: int
    epoch: object
    session: int
    cancel: threading.Event


class AudioJob(NamedTuple):
    player: str
    pcm: bytes
    ticket: VoiceTicket
    received: float


class SpeechJob(NamedTuple):
    text: str
    voice: str
    ticket: VoiceTicket
    queued: float


class CallJob(NamedTuple):
    text: str
    ticket: VoiceTicket


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
        self.local = PiperTTS(settings)

    def close(self):
        self.local.close()

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

    def transcribe(self, pcm, cancel=None):
        if cancel is not None and cancel.is_set():
            raise Cancelled("Распознавание отменено.")
        boundary = "joker" + uuid.uuid4().hex
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="model"\r\n\r\n{self.settings.stt_model}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.wav"\r\n'
                'Content-Type: audio/wav\r\n\r\n').encode() + wav_bytes(pcm) + f"\r\n--{boundary}--\r\n".encode()
        raw = self.request(self.settings.stt_url, self.settings.stt_key, body,
                           "multipart/form-data; boundary=" + boundary)
        if cancel is not None and cancel.is_set():
            raise Cancelled("Распознавание отменено.")
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or not isinstance(value.get("text"), str) or len(value["text"]) > 1000:
                raise ValueError()
            return value["text"].strip()
        except (ValueError, TypeError):
            raise ModelError("Распознавание вернуло некорректный текст.") from None

    def synthesize(self, text, voice, cancel=None):
        if self.settings.tts_backend == "piper":
            return self.local.synthesize(text, voice, cancel)
        if self.settings.tts_backend != "http":
            raise ValueError("TTS_BACKEND должен быть http или piper.")
        if cancel is not None and cancel.is_set():
            raise Cancelled("Синтез отменён.")
        raw = self.request(self.settings.tts_url, self.settings.tts_key,
            json.dumps({"model": self.settings.tts_model, "voice": voice, "input": text,
                        "response_format": "wav"}, ensure_ascii=False).encode(), "application/json")
        if cancel is not None and cancel.is_set():
            raise Cancelled("Синтез отменён.")
        return read_wav(raw)


class Voice:
    def __init__(self, app, provider=None):
        self.app = app
        self.provider = provider or AudioHTTP(app.settings)
        self.routes = AudioRoutes(app)
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
        self.mode = app.store.get("voice_mode", app.settings.voice_mode)
        self.preview_lock = threading.Lock()
        self.playback_lock = threading.Lock()

    def selected_voice(self):
        return self.voices.get(self.app.active, self.app.settings.tts_voice)

    def tts_status(self):
        s = self.app.settings
        voice = self.selected_voice()
        choices = installed(s) if s.tts_backend == "piper" else [
            {"id": v, "label": v} for v in sorted(set(self.voices.values()) | {s.tts_voice}) if v]
        import importlib.util
        ready = (bool(voice and s.tts_url and s.tts_model) if s.tts_backend == "http" else
                 s.tts_backend == "piper" and importlib.util.find_spec("piper") is not None
                 and voice in {v["id"] for v in choices})
        return {"backend": s.tts_backend, "ready": ready, "selected_voice": voice, "voices": choices,
                "daily_characters": s.tts_daily_characters}

    def status(self):
        with self.lock:
            return {"enabled": self.enabled, "detail": self.detail, "playing": self.playing,
                    "queued": self.speech.qsize(), "metrics": dict(self.metrics),
                    "role_voices": dict(self.voices), "mode": self.mode, "tts": self.tts_status(),
                    "output_device_configured": self.routes.configured("output"),
                    "stt_configured": bool(self.app.settings.stt_url and self.app.settings.stt_model)}

    def valid(self, revision, epoch):
        return (self.enabled and not self.shutdown.is_set() and not self.app.runtime.sleeping()
                and revision == self.app.store.get("response_revision", 0)
                and (epoch is None or epoch == self.app.minecraft.epoch and self.app.minecraft.entry is not None))

    def ticket(self, revision, epoch):
        return VoiceTicket(revision, epoch, self.session, self.cancel)

    def current(self, ticket):
        with self.lock:
            return (ticket.session == self.session and ticket.cancel is self.cancel
                    and not ticket.cancel.is_set() and self.valid(ticket.revision, ticket.epoch))

    def worker_current(self, expected_session):
        return expected_session is None or expected_session == self.session

    def feed(self, player, pcm, source="game", *, expected_session=None):
        if (self.mode == "tts" or not self.enabled or self.shutdown.is_set() or self.playing or self.app.runtime.sleeping()
                or not re.fullmatch(r"[A-Za-z0-9_]{3,16}", player)
                or player.casefold() == self.app.settings.minecraft_bot.casefold()
                or source != "call" and not self.app.settings.voice_guests and player.casefold() != self.app.settings.minecraft_owner.casefold()
                or source not in ("game", "call") or not 0 < len(pcm) <= 19200 or len(pcm) % 2):
            return
        now = time.monotonic()
        samples = struct.unpack("<" + "h" * (len(pcm) // 2), pcm)
        rms = math.sqrt(sum(x * x for x in samples) / len(samples))
        with self.lock:
            if not self.worker_current(expected_session) or not self.enabled or self.mode == "tts" or self.shutdown.is_set():
                return
            key = (source, player)
            buf = self.buffers.get(key)
            if rms < 250:
                return  # Silence is finalized by flush, not sent to a paid service.
            if buf is None:
                if len(self.buffers) >= 4:
                    return
                buf = {"pcm": bytearray(), "at": now, "last": now,
                       "ticket": self.ticket(self.app.store.get("response_revision", 0),
                                             None if source == "call" else self.app.minecraft.epoch)}
                self.buffers[key] = buf
            if len(buf["pcm"]) + len(pcm) > MAX_PCM:
                # Drop an overlong utterance as a whole instead of executing a truncated command.
                buf["overflow"] = True
            elif not buf.get("overflow"):
                buf["pcm"].extend(pcm)
            buf["last"] = now

    def flush(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            for key, buf in list(self.buffers.items()):
                if now - buf["last"] < .65:
                    continue
                del self.buffers[key]
                if not self.current(buf["ticket"]) or buf.get("overflow") or len(buf["pcm"]) < RATE * 2 * .3:
                    continue
                try:
                    self.audio.put_nowait(AudioJob(key[1], bytes(buf["pcm"]), buf["ticket"], buf["at"]))
                except queue.Full:
                    pass

    def listen_once(self, expected_session=None):
        if self.mode == "tts" or not self.worker_current(expected_session):
            return False
        self.flush()
        with self.lock:
            if not self.worker_current(expected_session):
                return False
            try:
                player, pcm, ticket, received = self.audio.get_nowait()
            except queue.Empty:
                return False
        with self.app.guard:
            if not self.current(ticket):
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
        try:
            text = self.provider.transcribe(pcm, ticket.cancel)
        except ModelError:
            if not self.current(ticket):
                return True
            raise
        with self.app.guard:
            if self.current(ticket) and text:
                self.metrics.update(intake_to_stt_ms=round((started - received) * 1000),
                                    stt_ms=round((time.monotonic() - started) * 1000))
                if ticket.epoch is None:
                    aliases = ("Джокер", self.app.masks[self.app.active]["display_name"])
                    if any(re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", text, re.I) for a in aliases):
                        try: self.calls.put_nowait(CallJob(text, ticket))
                        except queue.Full: pass
                else:
                    self.app.minecraft.accept({"user": player, "msg": text,
                        "parse_type": "parsed", "source": "voice"}, voice_ticket=ticket)
        return True

    def call_once(self, expected_session=None):
        if self.mode == "tts" or not self.worker_current(expected_session):
            return False
        with self.lock:
            if not self.worker_current(expected_session):
                return False
            try:
                text, ticket = self.calls.get_nowait()
            except queue.Empty:
                return False
        if not self.current(ticket):
            return True
        # An audio cable has no reliable individual speaker ID. It never has owner/tool rights.
        started = time.monotonic()
        result = self.app.reply(text, actor="call:shared", expected_revision=ticket.revision,
                                owner=False, cancel_event=ticket.cancel)
        if self.current(ticket):
            self.metrics["reply_ms"] = round((time.monotonic() - started) * 1000)
            self.enqueue(result["reply"], ticket.revision, None, ticket=ticket)
        return True

    def enqueue(self, text, revision, epoch, *, ticket=None):
        with self.lock:
            ticket = ticket or self.ticket(revision, epoch)
            if not self.current(ticket):
                return
            text = speech_text(text)
            if not text or len(text) > MAX_TEXT:
                self.detail = "Реплика не озвучена: нужен текст до 1000 символов. Полный ответ доступен в чате."
                return
            voice = self.selected_voice()
            try:
                self.speech.put_nowait(SpeechJob(text, voice, ticket, time.monotonic()))
            except queue.Full:
                self.detail = "Очередь голоса заполнена. Текст остаётся в игровом чате."

    def speak_once(self, expected_session=None):
        if not self.worker_current(expected_session):
            return False
        with self.lock:
            if not self.worker_current(expected_session):
                return False
            try:
                text, voice, ticket, queued = self.speech.get_nowait()
            except queue.Empty:
                return False
        if not self.current(ticket):
            return True
        cancel = ticket.cancel
        try:
            pcm, rate, channels = self.render(text, voice, cancel)
        except Cancelled:
            return True
        except ModelError:
            if not self.current(ticket):
                return True
            raise
        if not self.current(ticket):
            return True
        while not self.playback_lock.acquire(timeout=.05):
            if not self.current(ticket):
                return True
        try:
            return self.play(pcm, rate, channels, ticket, queued)
        finally:
            self.playback_lock.release()

    def play(self, pcm, rate, channels, ticket, queued):
        if not self.current(ticket):
            return True
        import sounddevice
        args = self.routes.stream_args("output", rate, channels)
        if not self.current(ticket):
            return True
        with sounddevice.RawOutputStream(**args) as output:
            self.playing = True
            try:
                chunk = max(2 * channels, rate // 50 * 2 * channels)
                for offset in range(0, len(pcm), chunk):
                    if not self.current(ticket):
                        output.abort()
                        break
                    if output.write(pcm[offset:offset + chunk]):
                        self.metrics["output_underflows"] = self.metrics.get("output_underflows", 0) + 1
                    if offset == 0:
                        self.metrics.update(text_to_sound_ms=round((time.monotonic() - queued) * 1000))
            finally:
                self.playing = False
        return True

    def render(self, text, voice, cancel):
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError("Для синтеза нужен текст от 1 до 1000 символов.")
        if cancel.is_set():
            raise Cancelled("Синтез отменён.")
        with self.app.guard:
            if cancel.is_set():
                raise Cancelled("Синтез отменён до резервирования бюджета.")
            day = self.app.store.now()[:10]
            budget = self.app.store.get("tts_budget", {"day": day, "used": 0})
            if budget["day"] != day:
                budget = {"day": day, "used": 0}
            if budget["used"] + len(text) > self.app.settings.tts_daily_characters:
                raise ModelError("Дневной лимит символов TTS исчерпан. Текстовый ответ остаётся доступен.")
            budget["used"] += len(text)
            self.app.store.set("tts_budget", budget)
        started = time.monotonic()
        pcm, rate, channels = self.provider.synthesize(text, voice, cancel)
        if cancel.is_set():
            raise Cancelled("Синтез отменён.")
        self.metrics.update(tts_ms=round((time.monotonic() - started) * 1000),
                            tts_audio_seconds=round(len(pcm) / rate / channels / 2, 2))
        return pcm, rate, channels

    def preview(self, text, mask=None):
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError("Для примера нужен текст от 1 до 1000 символов.")
        if not self.preview_lock.acquire(blocking=False):
            raise ModelError("Пример голоса уже готовится.")
        try:
            with self.app.guard:
                if mask is not None and mask != self.app.active:
                    raise Cancelled("Роль изменилась; обнови голосовой профиль.")
                cancel = self.cancel
                revision = self.app.store.get("response_revision", 0)
                voice = self.selected_voice()
            text = speech_text(text)
            if not text or not voice:
                raise ValueError("Нужны текст и выбранный голос.")
            pcm, rate, channels = self.render(text, voice, cancel)
            if cancel.is_set() or revision != self.app.store.get("response_revision", 0):
                raise Cancelled("Пример голоса отменён.")
            return pcm_wav(pcm, rate, channels)
        finally:
            self.preview_lock.release()

    def set_voice(self, voice, mask=None):
        if not isinstance(voice, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", voice):
            raise ValueError("Укажи ID голоса из своего TTS-сервиса или установленной модели Piper.")
        if self.app.settings.tts_backend == "piper" and voice not in {v["id"] for v in installed(self.app.settings)}:
            raise ValueError("Сначала установи этот голос Piper.")
        with self.app.guard:
            if mask is not None and mask != self.app.active:
                raise Cancelled("Роль изменилась; обнови голосовой профиль.")
            self.cancel_pending()
            self.voices[self.app.active] = voice
            self.app.store.set("role_voices", self.voices)

    def cancel_pending(self):
        with self.lock:
            self.cancel.set()
            self.cancel = threading.Event()
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
            self.set_voice(voice)
            return "Тембр сохранён для текущей маски."
        return self.detail

    def start(self, mode=None):
        with self.app.guard:
            return self._start(mode)

    def _start(self, mode=None):
        mode = mode or self.mode
        if mode not in ("tts", "conversation"):
            raise ValueError("Режим голоса: tts или conversation.")
        if self.enabled:
            if mode != self.mode:
                raise ValueError("Сначала выключи голос, затем выбери другой режим.")
            return
        s = self.app.settings
        if not self.tts_status()["ready"] or not self.routes.configured("output"):
            raise ModelError("Настрой TTS, выбранный голос и выход звука в панели. Пример можно создать без аудиоустройства.")
        if mode == "conversation" and not all((s.stt_url, s.stt_model)):
            raise ValueError("Для разговора настрой STT_URL/MODEL; для одной озвучки выбери режим tts.")
        import importlib.util
        if importlib.util.find_spec("sounddevice") is None:
            raise ValueError('Установи голос: pip install -e ".[voice]"')
        input_args = self.routes.preflight(mode)
        with self.lock:
            self.shutdown.clear()
            self.mode = mode
            self.session += 1
            self.enabled = True
        self.app.store.set("voice_mode", mode)
        self.detail = ("Озвучка включена; микрофон и STT не используются." if mode == "tts" else
                       "Голос включён. Обращайся по имени; вывод на выбранное аудиоустройство.")
        if input_args is not None:
            import sounddevice
            session = self.session
            def incoming(data, frames, timing, status):
                if session != self.session:
                    return
                if status:
                    with self.lock:
                        if session == self.session:
                            self.buffers.pop(("call", "CallGuest"), None)
                    return
                self.feed("CallGuest", bytes(data), source="call", expected_session=session)
            try:
                self.input_stream = sounddevice.RawInputStream(**input_args, blocksize=960, callback=incoming)
                self.input_stream.start()
            except Exception:
                self.close()
                raise ValueError("Не удалось открыть VOICE_INPUT_DEVICE, 48 кГц mono PCM16.") from None
        workers = [(self.speak_once, "joker-tts")]
        if mode == "conversation":
            workers.extend(((self.listen_once, "joker-stt"), (self.call_once, "joker-call")))
        self.threads = [threading.Thread(target=self.run, args=(fn, self.session), daemon=True, name=name)
                        for fn, name in workers]
        for thread in self.threads:
            thread.start()

    def run(self, step, session):
        while session == self.session and not self.shutdown.is_set():
            try:
                step(expected_session=session)
            except Cancelled:
                pass
            except ModelError as exc:
                with self.lock:
                    if session == self.session and not self.shutdown.is_set():
                        self.detail = str(exc)
            except Exception:
                with self.lock:
                    if session == self.session and not self.shutdown.is_set():
                        self.detail = "Ошибка аудио. Проверь сервис и устройство; текстовый чат доступен."
            self.shutdown.wait(.1)

    def close(self):
        with self.app.guard:
            self._close()

    def _close(self):
        with self.lock:
            self.session += 1
            self.enabled = False
            self.shutdown.set()
            self.cancel_pending()
        close_provider = getattr(self.provider, "close", None)
        cleanup_failed = False
        if callable(close_provider):
            try:
                close_provider()
            except Exception:
                cleanup_failed = True
        stream, self.input_stream = self.input_stream, None
        if stream is not None:
            # Even a disconnected device can reject abort; still release its native handle.
            for operation in (stream.abort, stream.close):
                try:
                    operation()
                except Exception:
                    cleanup_failed = True
        self.detail = ("Голос выключен. Драйвер или TTS-процесс не подтвердил полное закрытие; проверь устройство."
                       if cleanup_failed else "Голос выключен.")
