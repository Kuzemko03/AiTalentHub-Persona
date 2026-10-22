"""Private, offline native ASR process. No persona, memory, tokens, tools or microphone."""
import io
import json
from pathlib import Path
import sys
import wave

from .stt import MAX_WAV, audio_wav, pcm_wav


def recognize(model, raw, language, vocabulary=""):
    # Real-time packets may begin/end at speech; silence pads preserve edge phonemes.
    with wave.open(io.BytesIO(raw), "rb") as source:
        rate = source.getframerate()
        pcm = source.readframes(source.getnframes())
    pad = b"\x00\x00" * (rate // 4)
    raw = pcm_wav(pad + pcm + pad, rate)
    hint = (("Имена участников: " if language == "ru" else "Names of participants: ") + vocabulary + "."
            if vocabulary else None)
    segments, _ = model.transcribe(io.BytesIO(raw), language=None if language == "auto" else language,
        beam_size=5, temperature=0, condition_on_previous_text=False, vad_filter=True,
        vad_parameters={"min_silence_duration_ms": 350}, max_new_tokens=192, hotwords=hint)
    text = ""
    for segment in segments:
        text = (text + " " + segment.text.strip()).strip()
        if len(text) > 1000:
            raise ValueError("text_size")
    return {"text": text}


def main():
    from faster_whisper import WhisperModel
    cached = None
    while line := sys.stdin.buffer.readline(8193):
        try:
            if len(line) > 8192 or not line.endswith(b"\n"):
                raise ValueError("request_size")
            request = json.loads(line)
            threads, language = request["threads"], request["language"]
            vocabulary = request.get("vocabulary", "")
            if (type(threads) is not int or not 1 <= threads <= 16 or not isinstance(language, str)
                    or not isinstance(vocabulary, str) or len(vocabulary) > 200):
                raise ValueError("settings")
            directory, path = Path(request["model"]), Path(request["audio"])
            with path.open("rb") as audio:
                raw, _ = audio_wav(audio.read(MAX_WAV + 1))
            files = [directory / name for name in ("config.json", "model.bin", "tokenizer.json", "vocabulary.txt")]
            fingerprint = (str(directory), threads, tuple((p.stat().st_mtime_ns, p.stat().st_size) for p in files))
            if not cached or cached[0] != fingerprint:
                cached = None
                model = WhisperModel(str(directory), device="cpu", compute_type="int8", cpu_threads=threads,
                                     num_workers=1, local_files_only=True)
                cached = (fingerprint, model)
            result = recognize(cached[1], raw, language, vocabulary)
        except Exception:
            result = {"error": "recognition_failed"}
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
