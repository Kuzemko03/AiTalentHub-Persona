import argparse
import json
import shutil
import threading

from .backends import Cancelled, ModelError
from .config import Settings
from .core import App
from .panel import make_server
from .telegram_bot import TelegramError, TelegramRunner, pair


def main():
    parser = argparse.ArgumentParser(description="Джокер — персонаж со сменяемыми масками")
    parser.add_argument("mode", choices=["chat", "serve", "telegram", "pair", "doctor", "ask"], nargs="?", default="chat")
    parser.add_argument("text", nargs="?")
    parser.add_argument("--telegram", action="store_true", help="подключить Telegram вместе с панелью")
    args = parser.parse_args()
    settings = Settings.load()
    if args.mode == "pair":
        pair(settings)
        return
    if args.mode == "doctor":
        print(json.dumps({"backend": settings.backend, "model": settings.model,
                          "codex_found": bool(settings.codex_bin or shutil.which("codex")),
                          "telegram_token_configured": bool(settings.telegram_token),
                          "owner_configured": bool(settings.owner_id),
                          "panel": f"http://127.0.0.1:{settings.panel_port}",
                          "minecraft": "not connected"}, ensure_ascii=False, indent=2))
        return
    app = App(settings)
    if args.mode == "ask":
        if not args.text:
            parser.error("Для ask нужен текст сообщения.")
        print(json.dumps(app.handle(args.text), ensure_ascii=False, indent=2))
    elif args.mode == "chat":
        print("Джокер готов. /help — команды, /quit — выход.")
        while True:
            try:
                text = input("Вы: ").strip()
                if text == "/quit":
                    break
                if not text:
                    continue
                result = app.handle(text)
                print("Джокер:", result["reply"])
                if "usage" in result:
                    print(f"[{result['model']}; {result['latency']} сек.; токены: {json.dumps(result['usage'])}]")
            except (ModelError, ValueError) as exc:
                print(str(exc))
    elif args.mode == "telegram":
        TelegramRunner(app).run()
    else:
        server = make_server(app)
        runner = None
        if args.telegram:
            if not settings.owner_id:
                raise TelegramError("Перед запуском с Telegram задайте TELEGRAM_OWNER_ID через pair.")
            runner = TelegramRunner(app)
            threading.Thread(target=runner.run, daemon=True).start()
        print(f"Панель Джокера: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        finally:
            if runner:
                runner.shutdown.set()
            app.stop()
            server.server_close()


if __name__ == "__main__":
    try:
        main()
    except (ModelError, TelegramError, ValueError) as exc:
        print(str(exc))
        raise SystemExit(1) from None
    except (KeyboardInterrupt, EOFError):
        print("Джокер завершил работу.")
