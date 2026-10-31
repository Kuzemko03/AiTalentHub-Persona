"""Public implementation facts. No credentials, URLs, paths or private memories."""


def implementation(app):
    s = app.settings
    return {
        "runtime": "Own Python runtime with typed model-selected tools",
        "text_model": {"backend": s.backend, "model": s.model if s.backend == "codex" else s.api_model},
        "memory": "SQLite; common history across roles, separate private/game/call scopes",
        "minecraft": "Minecraft client with UnionClef, Py4J, parsed chat and player-tagged voice",
        "stt": {"backend": s.stt_backend, "model": s.stt_model, "language": s.stt_language,
                "execution": "local CPU int8" if s.stt_backend == "whisper" else "configured HTTP service"},
        "tts": {"backend": s.tts_backend, "voice": app.voice.selected_voice(),
                "execution": "local CPU" if s.tts_backend == "piper" else "configured HTTP service"},
        "pipeline": ["player audio from UnionClef", "speech segmentation and STT", "current persona and scoped memory",
                     "text model chooses typed tools", "reply in game chat", "TTS to selected audio device"],
        "voice_mode": app.voice.mode,
        "voice_duplex": "half-duplex: incoming speech is skipped while Joker speaks",
        "autonomy": "Goal, public observations, model-chosen next action, actual result checks, budgets and Stop",
    }
