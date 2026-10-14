"""Five REAL model calls using fictional inputs; saves a shareable verification record."""
import json
import shutil
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from joker.config import Settings
from joker.core import App


def main():
    settings = Settings.load()
    results = []
    with tempfile.TemporaryDirectory(prefix="joker-live-check-") as folder:
        root = Path(folder)
        shutil.copytree(settings.root / "agent", root / "agent")
        configured = replace(settings, root=root)
        app = App(configured)
        prompts = [("joker", "Меня зовут Никита. Я хочу построить дом в Minecraft. Запомни имя и цель. Кто ты?"),
                   ("housewife", "Я сгорел в лаве и потерял вещи. Что скажешь?"),
                   ("engineer", "Я сгорел в лаве и потерял вещи. Что скажешь?"),
                   ("locksmith", "Я сгорел в лаве и потерял вещи. Что скажешь?")]
        for mask, text in prompts:
            app.switch(mask)
            answer = app.reply(text)
            result = {"mask": mask, "input": text, **{k: v for k, v in answer.items() if not k.startswith("_")}}
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
        app.store.close()
        app = App(configured)
        restored = app.active == "locksmith"
        app.switch("joker")
        text = "Как меня зовут и что я хотел построить в Minecraft?"
        answer = app.reply(text)
        results.append({"mask": "joker", "after_restart": True, "input": text,
                        **{k: v for k, v in answer.items() if not k.startswith("_")}})
        passed = restored and "никит" in answer["reply"].casefold() and "дом" in answer["reply"].casefold()
        app.store.close()
    record = {"at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "provenance": "Real calls through the configured backend, fictional user Никита; no production dialogs.",
              "passed": passed, "restored_mask_after_restart": restored, "turns": results}
    output = settings.root / "docs" / "live-smoke.json"
    output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": passed, "evidence": str(output), "calls": len(results)}, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
