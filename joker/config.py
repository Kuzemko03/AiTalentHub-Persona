"""Local configuration. Secrets never enter character prompts or subprocess argv."""
import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def read_env(path: Path) -> dict[str, str]:
    result = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[key.strip()] = value
    return result


@dataclass
class Settings:
    root: Path = ROOT
    backend: str = "codex"
    model: str = "gpt-6-luna"
    reasoning: str = "none"
    codex_bin: str = ""
    timeout: int = 90
    telegram_token: str = ""
    owner_id: int = 0
    panel_port: int = 8765
    daily_requests: int = 100
    api_url: str = "https://api.mistral.ai/v1"
    api_model: str = ""
    api_key: str = ""
    minecraft_enabled: bool = False
    minecraft_owner: str = ""
    minecraft_bot: str = ""
    minecraft_port: int = 25333
    minecraft_callback_port: int = 25334
    minecraft_server: str = ""
    minecraft_autoconnect: bool = False
    runtime_interval: int = 60
    runtime_daily_requests: int = 20
    quiet_hours: str = ""
    utc_offset: int = 3
    voice_enabled: bool = False
    voice_guests: bool = False
    stt_url: str = ""
    stt_model: str = ""
    stt_key: str = ""
    tts_url: str = ""
    tts_model: str = ""
    tts_key: str = ""
    tts_voice: str = ""
    voice_device: str = ""
    voice_input_device: str = ""

    @classmethod
    def load(cls, root: Path = ROOT):
        values = {**read_env(root / ".env"), **os.environ}
        return cls(root=root, backend=values.get("MODEL_BACKEND", "codex"),
                   model=values.get("CODEX_MODEL", "gpt-6-luna"),
                   reasoning=values.get("CODEX_REASONING_EFFORT", "none"),
                   codex_bin=values.get("CODEX_BIN", ""),
                   timeout=int(values.get("CODEX_TIMEOUT", "90")),
                   telegram_token=values.get("TELEGRAM_BOT_TOKEN", ""),
                   owner_id=int(values.get("TELEGRAM_OWNER_ID") or 0),
                   panel_port=int(values.get("PANEL_PORT", "8765")),
                   daily_requests=int(values.get("MAX_DAILY_REQUESTS", "100")),
                   api_url=values.get("LLM_BASE_URL", "https://api.mistral.ai/v1"),
                   api_model=values.get("LLM_MODEL", ""), api_key=values.get("LLM_API_KEY", ""),
                   minecraft_enabled=values.get("MINECRAFT_ENABLED", "0").casefold() in ("1", "true"),
                   minecraft_owner=values.get("MINECRAFT_OWNER", ""),
                   minecraft_bot=values.get("MINECRAFT_BOT", ""),
                   minecraft_port=int(values.get("MINECRAFT_PORT", "25333")),
                   minecraft_callback_port=int(values.get("MINECRAFT_CALLBACK_PORT", "25334")),
                   minecraft_server=values.get("MINECRAFT_SERVER", ""),
                   minecraft_autoconnect=values.get("MINECRAFT_AUTOCONNECT", "0") == "1",
                   runtime_interval=int(values.get("RUNTIME_INTERVAL", "60")),
                   runtime_daily_requests=int(values.get("RUNTIME_DAILY_REQUESTS", "20")),
                   quiet_hours=values.get("QUIET_HOURS", ""),
                   utc_offset=int(values.get("UTC_OFFSET", "3")),
                   voice_enabled=values.get("VOICE_ENABLED", "0") == "1",
                   voice_guests=values.get("VOICE_GUESTS", "0") == "1",
                   stt_url=values.get("STT_URL", ""), stt_model=values.get("STT_MODEL", ""),
                   stt_key=values.get("STT_API_KEY", ""),
                   tts_url=values.get("TTS_URL", ""), tts_model=values.get("TTS_MODEL", ""),
                   tts_key=values.get("TTS_API_KEY", ""), tts_voice=values.get("TTS_VOICE", ""),
                   voice_device=values.get("VOICE_OUTPUT_DEVICE", ""),
                   voice_input_device=values.get("VOICE_INPUT_DEVICE", ""))
