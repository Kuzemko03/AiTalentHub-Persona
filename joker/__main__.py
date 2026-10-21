import argparse
import json
import shutil
import threading
import sys

from .backends import Cancelled, ModelError
from .config import Settings
from .core import App
from .panel import make_server
from .telegram_bot import TelegramError, TelegramRunner, pair
from .lifecycle import InstanceLock, snapshot


def main():
    if sys.argv[1:2] == ["channel"]:
        from .channel import cli
        cli(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description="Джокер — персонаж со сменяемыми масками")
    parser.add_argument("mode", choices=["chat", "serve", "telegram", "minecraft", "pair", "doctor", "ask", "backup", "audio-devices"], nargs="?", default="chat")
    parser.add_argument("text", nargs="?")
    parser.add_argument("--telegram", action="store_true", help="подключить Telegram вместе с панелью")
    parser.add_argument("--minecraft", action="store_true", help="подключить Minecraft через UnionClef")
    parser.add_argument("--voice", action="store_true", help="включить настроенное распознавание и синтез")
    args = parser.parse_args()
    settings = Settings.load()
    if args.mode == "audio-devices":
        try:
            import sounddevice
        except ImportError:
            raise ValueError('Установи голос: pip install -e ".[voice]"') from None
        for i, device in enumerate(sounddevice.query_devices()):
            print(f"{i}: {device['name']} (in={device['max_input_channels']}, out={device['max_output_channels']})")
        return
    if args.mode == "pair":
        pair(settings)
        return
    if args.mode == "doctor":
        from .minecraft import probe
        print(json.dumps({"backend": settings.backend, "model": settings.model,
                          "codex_found": bool(settings.codex_bin or shutil.which("codex")),
                          "telegram_token_configured": bool(settings.telegram_token),
                          "owner_configured": bool(settings.owner_id),
                          "panel": f"http://127.0.0.1:{settings.panel_port}",
                          "minecraft": probe(settings)}, ensure_ascii=False, indent=2))
        return
    lock = InstanceLock(settings.root)
    if args.mode in ("serve", "telegram", "minecraft", "chat"):
        lock.acquire()
    app = None
    try:
        app = App(settings)
        if args.mode == "backup":
            print(snapshot(app.store, settings.root))
            return
        if args.voice or settings.voice_enabled:
            app.voice.start()
        if args.minecraft or args.mode == "minecraft" or settings.minecraft_enabled:
            app.minecraft.start()
        if args.mode in ("serve", "telegram", "minecraft", "chat"):
            app.runtime.start()
        run_app(app, args, parser)
    finally:
        if app:
            app.runtime.close()
            if args.mode in ("serve", "telegram", "minecraft", "chat") or app.minecraft.thread is not None:
                # Preserve the user's autonomy preference for the next supervised start.
                app.cancel_reply()
                app.minecraft.stop_task()
            app.voice.close()
            app.minecraft.close()
        lock.close()


def run_app(app, args, parser):
    settings = app.settings
    if args.mode == "minecraft":
        print("Minecraft: ожидаю UnionClef на 127.0.0.1. Ctrl+C — выход.", flush=True)
        app.minecraft.shutdown.wait()
        return
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
            def run_telegram():
                try:
                    runner.run()
                except Exception as exc:
                    runner.state("failed", "Telegram остановлен; проверьте настройки и перезапустите")
                    print(f"Telegram остановлен ({type(exc).__name__}).", flush=True)
            threading.Thread(target=run_telegram, daemon=True).start()
        print(f"Панель Джокера: http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        finally:
            if runner:
                runner.shutdown.set()
            server.server_close()


if __name__ == "__main__":
    try:
        main()
    except (ModelError, TelegramError, ValueError) as exc:
        print(str(exc))
        raise SystemExit(1) from None
    except (KeyboardInterrupt, EOFError):
        print("Джокер завершил работу.")
