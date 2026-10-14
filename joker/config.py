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
    reasoning: str = "low"
    codex_bin: str = ""
    timeout: int = 90
    telegram_token: str = ""
    owner_id: int = 0
    panel_port: int = 8765
    daily_requests: int = 100
    api_url: str = "https://api.mistral.ai/v1"
    api_model: str = ""
    api_key: str = ""

    @classmethod
    def load(cls, root: Path = ROOT):
        values = {**read_env(root / ".env"), **os.environ}
        return cls(root=root, backend=values.get("MODEL_BACKEND", "codex"),
                   model=values.get("CODEX_MODEL", "gpt-6-luna"),
                   reasoning=values.get("CODEX_REASONING_EFFORT", "low"),
                   codex_bin=values.get("CODEX_BIN", ""),
                   timeout=int(values.get("CODEX_TIMEOUT", "90")),
                   telegram_token=values.get("TELEGRAM_BOT_TOKEN", ""),
                   owner_id=int(values.get("TELEGRAM_OWNER_ID") or 0),
                   panel_port=int(values.get("PANEL_PORT", "8765")),
                   daily_requests=int(values.get("MAX_DAILY_REQUESTS", "100")),
                   api_url=values.get("LLM_BASE_URL", "https://api.mistral.ai/v1"),
                   api_model=values.get("LLM_MODEL", ""), api_key=values.get("LLM_API_KEY", ""))
