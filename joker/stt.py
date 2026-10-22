"""Opt-in local speech recognition; downloads are explicit, native inference is isolated."""
import argparse
from dataclasses import replace
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
import wave

from .backends import Cancelled, ModelError

MAX_WAV = 1_000_000
RATE = 48000
MAX_SECONDS = 10


def audio_wav(raw):
    """Bounded PCM16 mono WAV, canonicalized to remove unused chunks/metadata."""
    if not isinstance(raw, bytes) or not 44 <= len(raw) <= MAX_WAV:
        raise ValueError("Нужен WAV PCM16 mono, от 0,1 до 10 секунд, до 1 МБ.")
    try:
        with wave.open(io.BytesIO(raw), "rb") as source:
            rate, frames = source.getframerate(), source.getnframes()
            if (source.getnchannels() != 1 or source.getsampwidth() != 2 or source.getcomptype() != "NONE"
                    or not 8000 <= rate <= RATE or not rate // 10 <= frames <= rate * MAX_SECONDS):
                raise ValueError()
            pcm = source.readframes(frames)
            if len(pcm) != frames * 2:
                raise ValueError()
    except (wave.Error, EOFError, ValueError, OSError):
        raise ValueError("Нужен целый WAV PCM16 mono, от 0,1 до 10 секунд, 8–48 кГц.") from None
    return pcm_wav(pcm, rate), frames / rate


def pcm_wav(pcm, rate=RATE):
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(rate)
        target.writeframes(pcm)
    return output.getvalue()


def catalog(settings):
    return json.loads((settings.root / "agent/shared/stt-models.json").read_text(encoding="utf-8"))


def model_dir(settings, model_id):
    if not isinstance(model_id, str) or not re.fullmatch(r"[a-z0-9_-]{1,50}", model_id):
        raise ValueError("Укажи ID модели распознавания из каталога.")
    root = (Path(settings.stt_models_dir).expanduser() if settings.stt_models_dir else
            settings.root / "data/stt/models").resolve()
    target = (root / model_id).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Модель выходит за каталог распознавания.")
    return target


def installed(settings):
    models = []
    for entry in catalog(settings):
        try:
            directory = model_dir(settings, entry["id"])
            if all((directory / f["name"]).resolve().is_relative_to(directory)
                   and (directory / f["name"]).is_file()
                   and (directory / f["name"]).stat().st_size == f["size"] for f in entry["files"]):
                models.append({"id": entry["id"], "label": entry["label"]})
        except (OSError, ValueError, KeyError):
            continue
    return models


def status(settings):
    local = settings.stt_backend == "whisper"
    ready = (importlib.util.find_spec("faster_whisper") is not None and
             settings.stt_model in {m["id"] for m in installed(settings)}) if local else (
             settings.stt_backend == "http" and bool(settings.stt_url and settings.stt_model))
    return {"backend": settings.stt_backend, "ready": ready, "model": settings.stt_model,
            "language": settings.stt_language, "threads": settings.stt_threads,
            "daily_requests": settings.stt_daily_requests}


def download(settings, model_id):
    entry = next((m for m in catalog(settings) if m["id"] == model_id), None)
    if entry is None:
        raise ValueError("Такой модели нет в каталоге распознавания.")
    root = model_dir(settings, model_id)
    root.mkdir(parents=True, exist_ok=True)
    pending = []
    try:
        for spec in entry["files"]:
            if (spec["name"] not in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt", "README.md")
                    or not spec["url"].startswith("https://huggingface.co/Systran/faster-whisper-" + model_id
                                                 + "/resolve/" + entry["revision"] + "/")
                    or type(spec["size"]) is not int or not 0 < spec["size"] <= 600_000_000
                    or not re.fullmatch(r"[0-9a-f]{64}", spec["sha256"])):
                raise ValueError("Повреждён каталог моделей распознавания.")
            target = root / spec["name"]
            if not target.resolve().is_relative_to(root):
                raise ValueError("Файл выходит за каталог модели.")
            if target.is_file() and target.stat().st_size == spec["size"]:
                with target.open("rb") as existing:
                    if hashlib.file_digest(existing, "sha256").hexdigest() == spec["sha256"]:
                        continue
            temporary = root / ("." + uuid.uuid4().hex + ".part")
            pending.append((temporary, target))
            digest, size = hashlib.sha256(), 0
            with urllib.request.urlopen(spec["url"], timeout=30) as response, temporary.open("wb") as output:
                while data := response.read(1024 * 1024):
                    size += len(data)
                    if size > spec["size"]:
                        raise ValueError("Модель превышает ожидаемый размер.")
                    output.write(data)
                    digest.update(data)
            if size != spec["size"] or digest.hexdigest() != spec["sha256"]:
                raise ValueError("Контрольная сумма модели не совпала. Файл не установлен.")
        for temporary, target in pending:
            temporary.replace(target)
    except OSError:
        raise ModelError("Модель STT не загружена. Проверь соединение и каталог моделей.") from None
    finally:
        for temporary, _ in pending:
            temporary.unlink(missing_ok=True)
    return entry


class WhisperSTT:
    MAX_RESPONSE = 16000

    def __init__(self, settings):
        self.settings = settings
        self.lock = threading.Lock()
        self.process_guard = threading.Lock()
        self.worker = None

    def close(self):
        with self.process_guard:
            worker, self.worker = self.worker, None
        if worker:
            process, _, reader = worker
            if process.poll() is None:
                process.kill()
            process.wait(timeout=3)
            reader.join(timeout=1)
            for stream in (process.stdin, process.stdout):
                stream.close()

    def start_worker(self):
        with self.process_guard:
            if self.worker is not None:
                return self.worker
            allowed = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME"}
            env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
            env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", HF_HUB_OFFLINE="1",
                       HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false")
            process = subprocess.Popen([sys.executable, "-m", "joker.stt_worker"],
                cwd=Path(__file__).resolve().parent.parent, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            replies = queue.Queue(maxsize=1)
            def read():
                try:
                    while line := process.stdout.readline(self.MAX_RESPONSE + 1):
                        if len(line) > self.MAX_RESPONSE or not line.endswith(b"\n"):
                            replies.put_nowait(None)
                            return
                        replies.put_nowait(line)
                except (OSError, ValueError, queue.Full):
                    pass
                finally:
                    try: replies.put_nowait(None)
                    except queue.Full: pass
            reader = threading.Thread(target=read, daemon=True, name="joker-whisper-reader")
            reader.start()
            self.worker = (process, replies, reader)
            return self.worker

    def transcribe(self, pcm, cancel=None, vocabulary=""):
        return self.transcribe_wav(pcm_wav(pcm), cancel, vocabulary)

    def transcribe_wav(self, raw, cancel=None, vocabulary=""):
        cancel = cancel or threading.Event()
        started = time.monotonic()
        def check():
            if cancel.is_set():
                raise Cancelled("Распознавание отменено.")
            if time.monotonic() - started > 60:
                raise ModelError("Локальное распознавание превысило 60 секунд.")
        check()
        raw, _ = audio_wav(raw)
        s = self.settings
        if not status(s)["ready"]:
            raise ModelError('Установи распознавание: pip install -e ".[stt]", затем joker stt download small.')
        if type(s.stt_threads) is not int or not 1 <= s.stt_threads <= 16 or not re.fullmatch(r"[a-z]{2,3}|auto", s.stt_language):
            raise ValueError("STT_THREADS: 1–16; STT_LANGUAGE: код языка или auto.")
        if not isinstance(vocabulary, str) or len(vocabulary) > 200:
            raise ValueError("Словарь имён для STT должен быть строкой до 200 символов.")
        while not self.lock.acquire(timeout=.05):
            check()
        audio_path = None
        try:
            check()
            directory = s.root / "data/stt/jobs"
            directory.mkdir(parents=True, exist_ok=True)
            # Small pipe requests remain cancellable even while native imports/load are busy.
            with tempfile.NamedTemporaryFile(dir=directory, suffix=".wav", delete=False) as audio:
                audio_path = Path(audio.name)
                audio.write(raw)
            process, replies, _ = self.start_worker()
            request = dict(audio=str(audio_path.resolve()), model=str(model_dir(s, s.stt_model)),
                           language=s.stt_language, threads=s.stt_threads, vocabulary=vocabulary)
            check()
            payload = json.dumps(request, ensure_ascii=True).encode() + b"\n"
            if len(payload) > 4096:
                raise ModelError("Путь к модели или записи слишком длинный для локального STT.")
            process.stdin.write(payload)
            process.stdin.flush()
            while True:
                check()
                try:
                    line = replies.get(timeout=.05)
                    break
                except queue.Empty:
                    continue
            check()
            if line is None:
                raise ModelError("Процесс Whisper остановился. Проверь модель и установку STT.")
            result = json.loads(line)
            if (not isinstance(result, dict) or "error" in result or not isinstance(result.get("text"), str)
                    or len(result["text"]) > 1000):
                raise ModelError("Whisper не подтвердил распознавание. Проверь модель и формат записи.")
            return result["text"].strip()
        except (Cancelled, ModelError):
            self.close()
            raise
        except Exception:
            self.close()
            raise ModelError("Не удалось распознать речь локально. Проверь модель и зависимость faster-whisper.") from None
        finally:
            try:
                if audio_path is not None:
                    audio_path.unlink(missing_ok=True)
            finally:
                self.lock.release()


def cli(arguments, settings):
    parser = argparse.ArgumentParser(description="Локальное распознавание Джокера; микрофон не открывается")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("models")
    get = commands.add_parser("download")
    get.add_argument("model")
    transcribe = commands.add_parser("transcribe")
    transcribe.add_argument("audio", type=Path)
    transcribe.add_argument("--model", default=settings.stt_model if settings.stt_backend == "whisper" else "small")
    transcribe.add_argument("--vocabulary", default="", help="публичные имена, до 200 символов; подсказка модели")
    args = parser.parse_args(arguments)
    if args.action == "models":
        print(json.dumps({"installed": installed(settings), "downloadable": [
            {"id": m["id"], "label": m["label"]} for m in catalog(settings)]}, ensure_ascii=False, indent=2))
    elif args.action == "download":
        print("Модель установлена: " + download(settings, args.model)["id"])
    else:
        with args.audio.open("rb") as audio:
            raw = audio.read(MAX_WAV + 1)
        provider, started = WhisperSTT(replace(settings, stt_backend="whisper", stt_model=args.model)), time.monotonic()
        try:
            text = provider.transcribe_wav(raw, vocabulary=args.vocabulary)
        finally:
            provider.close()
        print(json.dumps({"text": text, "milliseconds": round((time.monotonic() - started) * 1000)}, ensure_ascii=False))
