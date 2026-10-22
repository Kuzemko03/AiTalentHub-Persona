"""Explicit native ASR smoke test on generated speech; no microphone, game or chat history."""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import importlib.metadata
import json
from pathlib import Path
import random
import re
import struct
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from joker.backends import Cancelled
from joker.config import Settings
from joker.stt import WhisperSTT, pcm_wav
from joker.tts import PiperTTS, pcm_wav as tts_wav


def words(text):
    return re.findall(r"\w+", text.casefold().replace("ё", "е"))


def errors(reference, text):
    expected, actual = words(reference), words(text)
    previous = list(range(len(actual)+1))
    for i, word in enumerate(expected, 1):
        current = [i]
        for j, other in enumerate(actual, 1):
            current.append(min(current[-1]+1, previous[j]+1, previous[j-1]+(word != other)))
        previous = current
    return previous[-1], len(expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("data/stt/smoke-v09/report.json"))
    args = parser.parse_args()
    s = replace(Settings.load(), stt_backend="whisper", stt_model="small", tts_backend="piper")
    name = json.loads((s.root / "agent/joker/masks/joker.json").read_text(encoding="utf-8"))["display_name"]
    directory = s.root / "data/stt/smoke-v09"
    directory.mkdir(parents=True, exist_ok=True)
    tts, stt = PiperTTS(s), WhisperSTT(s)
    report = {"at": datetime.now(timezone.utc).isoformat(), "model": "small", "language": s.stt_language,
              "device": "cpu", "compute_type": "int8", "threads": s.stt_threads,
              "packages": {p: importlib.metadata.version(p) for p in ("faster-whisper", "ctranslate2", "av", "piper-tts")},
              "audio_source": "synthetic Piper speech and generated silence/noise; no microphone",
              "game_tested": False, "cases": []}
    fixtures = [("denis-address", "ru_RU-denis-medium", f"{name}, привет! Расскажи, что ты умеешь."),
                ("irina-role", "ru_RU-irina-medium", f"{name}, давай сегодня поговорим с домохозяйкой."),
                ("denis-task", "ru_RU-denis-medium", "Сначала собери дерево, потом подойди ко мне."),
                ("irina-stop", "ru_RU-irina-medium", f"{name}, остановись, пожалуйста.")]
    try:
        for id, voice, reference in fixtures:
            pcm, rate, channels = tts.synthesize(reference, voice)
            raw = tts_wav(pcm, rate, channels)
            (directory / (id+".wav")).write_bytes(raw)
            started = time.monotonic()
            text = stt.transcribe_wav(raw, vocabulary=name)
            wrong, total = errors(reference, text)
            row = {"id": id, "reference": reference, "text": text,
                   "milliseconds": round((time.monotonic()-started)*1000), "word_errors": wrong, "words": total}
            report["cases"].append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
        rng = random.Random(2026)
        for id, pcm in (("silence", b"\x00\x00"*144000),
                        ("white-noise", b"".join(struct.pack("<h", rng.randint(-2000, 2000)) for _ in range(144000)))):
            started = time.monotonic()
            text = stt.transcribe_wav(pcm_wav(pcm), vocabulary=name)
            report["cases"].append({"id": id, "text": text, "expected_empty": True,
                                    "milliseconds": round((time.monotonic()-started)*1000)})
        cancel = threading.Event()
        outcome = []
        def request():
            try:
                stt.transcribe_wav((directory / "denis-address.wav").read_bytes(), cancel, name)
                outcome.append("finished")
            except Cancelled:
                outcome.append("cancelled")
            except Exception as exc:
                outcome.append(type(exc).__name__)
        worker = threading.Thread(target=request, daemon=True)
        worker.start()
        cancel.wait(.1)
        started = time.monotonic()
        cancel.set()
        worker.join(3)
        report["cancellation"] = {"outcome": outcome, "milliseconds": round((time.monotonic()-started)*1000),
                                  "worker_closed": stt.worker is None, "thread_finished": not worker.is_alive()}
    finally:
        tts.close()
        stt.close()
    report["passed"] = (all(c.get("word_errors", 0) == 0 and (not c.get("expected_empty") or not c["text"])
                             for c in report["cases"]) and report["cancellation"]["outcome"] == ["cancelled"]
                        and report["cancellation"]["worker_closed"] and report["cancellation"]["thread_finished"])
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print("REPORT="+str(args.report.resolve()), flush=True)
    if not report["passed"]:
        raise SystemExit("Native STT smoke check needs review; see report.")


if __name__ == "__main__":
    main()
