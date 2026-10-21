"""Private stdin/stdout protocol. Native TTS crashes cannot terminate the application."""
import base64
import ctypes
import json
import os
from pathlib import Path
import sys


def native_path(path):
    """espeak-ng on Windows needs a narrow-character path; prefer the NTFS alias."""
    path = str(path)
    if os.name != "nt" or path.isascii():
        return path
    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.kernel32.GetShortPathNameW(path, buffer, len(buffer))
    if not result or result >= len(buffer) or not buffer.value.isascii():
        raise ValueError("espeak_data_path_requires_ascii")
    return buffer.value


def synthesize(voice, text):
    pcm, audio_format, segment = bytearray(), None, ""
    segments = []
    for word in text.split():
        if len(word) > 240:
            raise ValueError("text_segment_too_long")
        if len(segment) + len(word) + 1 > 240:
            segments.append(segment)
            segment = ""
        segment = (segment + " " + word).strip()
    if segment:
        segments.append(segment)
    for segment in segments:
        for chunk in voice.synthesize(segment):
            current = (chunk.sample_rate, chunk.sample_channels)
            if chunk.sample_width != 2 or current[1] not in (1, 2) or not 8000 <= current[0] <= 48000:
                raise ValueError("audio_format")
            if audio_format is not None and current != audio_format:
                raise ValueError("audio_format_changed")
            audio_format = current
            pcm.extend(chunk.audio_int16_bytes)
            if len(pcm) % (2 * current[1]) or len(pcm) > min(8000000, current[0] * current[1] * 2 * 60):
                raise ValueError("audio_size")
    if not pcm or audio_format is None:
        raise ValueError("empty_audio")
    return {"pcm": base64.b64encode(pcm).decode("ascii"), "rate": audio_format[0], "channels": audio_format[1]}


def main():
    import piper
    from piper import PiperVoice
    cached = None
    data_dir = native_path(Path(piper.__file__).parent / "espeak-ng-data")
    for line in sys.stdin.buffer:
        try:
            if len(line) > 20000:
                raise ValueError("request_size")
            request = json.loads(line)
            text = request["text"]
            if not isinstance(text, str) or not text.strip() or len(text) > 1000:
                raise ValueError("text_size")
            model, config = Path(request["model"]), Path(request["config"])
            fingerprint = (str(model), model.stat().st_mtime_ns, model.stat().st_size,
                           config.stat().st_mtime_ns, config.stat().st_size)
            if not cached or cached[0] != fingerprint:
                cached = None
                cached = (fingerprint, PiperVoice.load(str(model), config_path=str(config),
                            use_cuda=False, espeak_data_dir=data_dir, download_dir=model.parent))
            result = synthesize(cached[1], text)
        except Exception:
            result = {"error": "synthesis_failed"}
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    main()
