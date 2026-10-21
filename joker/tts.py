"""Optional local Piper speech. Models are explicit, local assets, never prompt tools."""
import argparse
import base64
import os
import queue
import subprocess
import sys
import hashlib
import importlib.util
import io
import json
import re
import threading
import time
import urllib.request
import uuid
import wave
from pathlib import Path

from .backends import Cancelled, ModelError

VOICE_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
MAX_TEXT = 1000


def speech_text(text):
    """Render Markdown for speech; this does not interpret the user's intentions."""
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    text = re.sub(r"!?\[([^\]]+)\]\([^\s)]+\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    return " ".join(re.sub(r"[`*_#]", "", text).split())


def pcm_wav(pcm, rate, channels):
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(channels)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(pcm)
    return stream.getvalue()


def model_dir(settings):
    path = Path(settings.tts_voices_dir) if settings.tts_voices_dir else settings.root / "data" / "voices"
    return path.resolve()


def voice_files(settings, voice_id):
    if not isinstance(voice_id, str) or not VOICE_ID.fullmatch(voice_id):
        raise ValueError("Нужен ID установленного голоса Piper, без пути к файлу.")
    root = model_dir(settings)
    files = [root / (voice_id + suffix) for suffix in (".onnx", ".onnx.json")]
    if any(not file.resolve().is_relative_to(root) for file in files):
        raise ValueError("Файлы голоса должны находиться в каталоге моделей.")
    return files


def installed(settings):
    voices = []
    try:
        labels = {v["id"]: v["label"] for v in catalog(settings)}
    except (OSError, ValueError, KeyError, TypeError):
        labels = {}
    for file in sorted(model_dir(settings).glob("*.onnx")):
        if len(voices) >= 64:
            break
        try:
            model, config = voice_files(settings, file.stem)
            if not model.is_file() or not config.is_file() or config.stat().st_size > 1000000:
                continue
            data = json.loads(config.read_text(encoding="utf-8"))
            rate = data["audio"]["sample_rate"]
            if (type(rate) is not int or not 8000 <= rate <= 48000
                    or type(data.get("num_speakers", 1)) is not int or data.get("num_speakers", 1) != 1):
                continue
            voices.append({"id": file.stem, "label": labels.get(file.stem, file.stem), "sample_rate": rate})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return voices


class PiperTTS:
    MAX_RESPONSE = 12000000

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
            # Credentials and provider settings never enter the native speech worker.
            allowed = {"SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "USERPROFILE", "LOCALAPPDATA", "APPDATA", "HOME"}
            env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
            env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
            process = subprocess.Popen([sys.executable, "-m", "joker.tts_worker"],
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
                    try:
                        replies.put_nowait(None)
                    except queue.Full:
                        pass
            reader = threading.Thread(target=read, daemon=True, name="joker-piper-reader")
            reader.start()
            self.worker = (process, replies, reader)
            return self.worker

    def synthesize(self, text, voice_id, cancel=None):
        cancel = cancel or threading.Event()
        started = time.monotonic()
        def check():
            if cancel.is_set():
                raise Cancelled("Синтез отменён.")
            if time.monotonic() - started > 60:
                raise ModelError("Локальный синтез превысил 60 секунд.")
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ValueError("Для синтеза нужен текст от 1 до 1000 символов.")
        model, config = voice_files(self.settings, voice_id)
        check()
        if voice_id not in {voice["id"] for voice in installed(self.settings)}:
            raise ModelError("Голос Piper не установлен. Сначала загрузи модель командой joker tts download.")
        if importlib.util.find_spec("piper") is None:
            raise ModelError('Установи локальный синтез: pip install -e ".[tts]"')
        while not self.lock.acquire(timeout=.1):
            check()
        try:
            check()
            process, replies, _ = self.start_worker()
            request = json.dumps({"model": str(model), "config": str(config), "text": text}, ensure_ascii=True).encode()+b"\n"
            check()
            process.stdin.write(request)
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
                raise ModelError("Процесс Piper остановился. Проверь установку и доступность словаря произношения.")
            result = json.loads(line)
            if "error" in result:
                raise ModelError("Piper не смог создать звук. Проверь модель и каталог произношения.")
            pcm = base64.b64decode(result["pcm"], validate=True)
            rate, channels = result["rate"], result["channels"]
            if (type(rate) is not int or type(channels) is not int or not 8000 <= rate <= 48000
                    or channels not in (1, 2) or not pcm or len(pcm) % (2*channels)
                    or len(pcm) > min(8000000, rate*channels*2*60)):
                raise ModelError("Piper вернул неподдерживаемый звук.")
            return pcm, rate, channels
        except (Cancelled, ModelError):
            self.close()
            raise
        except Exception:
            self.close()
            raise ModelError("Не удалось синтезировать локальный голос. Проверь модель и зависимость Piper.") from None
        finally:
            self.lock.release()


def catalog(settings):
    return json.loads((settings.root / "agent/shared/tts-voices.json").read_text(encoding="utf-8"))


def download(settings, voice_id):
    """Explicit CLI operation, pinned public models with SHA-256; no auto-download in playback."""
    entry = next((v for v in catalog(settings) if v["id"] == voice_id), None)
    if entry is None:
        raise ValueError("Такого голоса нет в каталоге загрузки.")
    voice_files(settings, voice_id)
    root = model_dir(settings)
    root.mkdir(parents=True, exist_ok=True)
    pending = []
    try:
        for spec in entry["files"]:
            if (Path(spec["name"]).name != spec["name"] or not spec["name"].startswith(voice_id + ".")
                    or not spec["url"].startswith("https://huggingface.co/rhasspy/piper-voices/resolve/" + entry["revision"] + "/")
                    or type(spec["size"]) is not int or not 0 < spec["size"] <= 200000000
                    or not re.fullmatch(r"[0-9a-f]{64}", spec["sha256"])):
                raise ValueError("Повреждён каталог загрузки голосов.")
            target = root / spec["name"]
            if not target.resolve().is_relative_to(root):
                raise ValueError("Файл модели выходит за каталог голосов.")
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
                        raise ValueError("Загруженная модель превышает ожидаемый размер.")
                    output.write(data)
                    digest.update(data)
            if size != spec["size"] or digest.hexdigest() != spec["sha256"]:
                raise ValueError("Контрольная сумма модели не совпала. Файл не установлен.")
        for temporary, target in pending:
            temporary.replace(target)
    except OSError:
        raise ModelError("Не удалось загрузить модель голоса. Проверь соединение и каталог моделей.") from None
    finally:
        for temporary, _ in pending:
            temporary.unlink(missing_ok=True)
    return entry


def cli(arguments, settings):
    parser = argparse.ArgumentParser(description="Локальные голоса Джокера; звук не воспроизводится")
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("voices")
    get = commands.add_parser("download")
    get.add_argument("voice")
    render = commands.add_parser("render")
    render.add_argument("--voice", required=True)
    render.add_argument("--text", required=True)
    render.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(arguments)
    if args.action == "voices":
        print(json.dumps({"installed": installed(settings), "downloadable": [
            {"id": v["id"], "label": v["label"]} for v in catalog(settings)]}, ensure_ascii=False, indent=2))
    elif args.action == "download":
        print("Голос установлен: " + download(settings, args.voice)["id"])
    else:
        if args.output.exists():
            raise ValueError("Файл примера уже существует. Выбери другое имя, чтобы сохранить прежнюю запись.")
        provider = PiperTTS(settings)
        try:
            pcm, rate, channels = provider.synthesize(speech_text(args.text), args.voice)
        finally:
            provider.close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("xb") as output:
            output.write(pcm_wav(pcm, rate, channels))
        print(json.dumps({"output": str(args.output.resolve()), "seconds": round(len(pcm) / rate / channels / 2, 2),
                          "sample_rate": rate}, ensure_ascii=False))
