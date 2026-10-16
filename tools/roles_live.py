"""Opt-in real checks: fictional inputs, temporary DB, no Telegram publication."""
import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from joker.config import ROOT, Settings
from joker.core import App


def run():
    records = []
    with tempfile.TemporaryDirectory(prefix="joker-roles-live-") as folder:
        root = Path(folder)
        shutil.copytree(ROOT / "agent", root / "agent")
        shutil.copytree(ROOT / "channel", root / "channel")
        app = App(replace(Settings.load(), root=root))
        try:
            app.store.remember("owner", "Меня зовут Тимофей")
            for role_id in ("plumber", "housewife"):
                app.switch(role_id)
                result = app.reply("Как тебя зовут и чем занимаешься? Ответь как в обычном разговоре.")
                records.append({"case": role_id, "reply": result["reply"], "latency": result["latency"]})
                print(json.dumps(records[-1], ensure_ascii=False), flush=True)
            command = "Джокер, стань экскурсоводом Ниной, 41 год. Спокойная, любит замечать смешные бытовые детали. Рыжие волосы, веснушки, синяя куртка."
            confirmation = app.handle(command)["reply"]
            result = app.reply("Нина, кто ты и как меня зовут? Только без экскурсии: мы пока знакомимся.")
            records.append({"case": "new_role", "request": command, "confirmation": confirmation,
                            "profile": app.masks[app.active], "reply": result["reply"], "latency": result["latency"]})
            print(json.dumps(records[-1], ensure_ascii=False), flush=True)
            app.switch("plumber")
            draft = app.channel.generate(topic="Пришёл чинить капающий кран, а хозяин заранее разобрал полкухни. Сухой бытовой юмор, без технической инструкции.")
            records.append({"case": "plumber_draft_only", "author": draft["author"], "title": draft["title"],
                            "text": draft["text"], "image_prompt": draft["image_prompt"], "status": draft["status"]})
            print(json.dumps(records[-1], ensure_ascii=False), flush=True)
        finally:
            app.store.close()
    (ROOT / "docs/roles-live.json").write_text(json.dumps({"fictional_inputs_only": True,
        "published": False, "results": records}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    run()
