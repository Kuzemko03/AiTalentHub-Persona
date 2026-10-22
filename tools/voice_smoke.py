"""Real file -> local STT -> configured persona model -> local TTS, with no game/mic/playback."""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from joker.config import Settings
from joker.core import App
from joker.tts import pcm_wav


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--report", type=Path, default=Path("data/stt/smoke-v09/voice-report.json"))
    args = parser.parse_args()
    from faster_whisper.audio import decode_audio
    settings = Settings.load()
    with args.audio.open("rb") as source:
        raw = source.read(1_000_001)
    from joker.stt import audio_wav
    raw, _ = audio_wav(raw)
    samples = decode_audio(io.BytesIO(raw), sampling_rate=48000)
    pcm = (samples*32768).clip(-32768, 32767).astype("<i2").tobytes()
    with tempfile.TemporaryDirectory(prefix="joker-voice-smoke-") as folder:
        root = Path(folder)
        shutil.copytree(settings.root / "agent", root / "agent")
        s = replace(settings, root=root, stt_backend="whisper", stt_model="small", tts_backend="piper",
                    stt_models_dir=str(settings.root / "data/stt/models"),
                    tts_voices_dir=str(settings.root / "data/voices"), tts_voice="ru_RU-denis-medium",
                    voice_mode="conversation", voice_enabled=False, minecraft_enabled=False)
        app = App(s)
        try:
            app.voice.enabled = True  # Drive the actual stages without creating capture/playback threads.
            for offset in range(0, len(pcm), 1920):
                app.voice.feed("CallGuest", pcm[offset:offset+1920], "call")
            app.voice.flush(time.monotonic()+1)
            if not app.voice.listen_once() or app.voice.calls.empty():
                raise RuntimeError("ASR did not produce an addressed utterance; inspect the source audio.")
            with app.voice.calls.mutex:
                recognized = app.voice.calls.queue[0].text
            if not app.voice.call_once() or app.voice.speech.empty():
                raise RuntimeError("The persona did not produce a spoken reply.")
            job = app.voice.speech.get_nowait()
            pcm, rate, channels = app.voice.render(job.text, job.voice, job.ticket.cancel)
            output = settings.root / "data/stt/smoke-v09/joker-reply.wav"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(pcm_wav(pcm, rate, channels))
            report = {"at": datetime.now(timezone.utc).isoformat(), "passed": True,
                      "provenance": "Synthetic input audio, real configured text model, local Whisper and Piper; temporary empty history.",
                      "microphone_tested": False, "minecraft_tested": False, "playback_tested": False,
                      "actor": "call:shared", "recognized": recognized, "reply": job.text,
                      "metrics": app.voice.metrics, "text_model": app.store.get("last_call", {}).get("model"),
                      "reply_audio_seconds": round(len(pcm)/rate/channels/2, 2)}
        finally:
            app.voice.close()
            app.runtime.close()
            app.minecraft.close()
            app.store.close()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
