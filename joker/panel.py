"""Loopback-only operator panel; no filesystem or shell endpoints."""
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .backends import Cancelled, ModelError


def make_server(app, port=None):
    csrf = secrets.token_urlsafe(32)
    html_path = app.settings.root / "joker" / "panel.html"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def allowed_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}",
                                                f"localhost:{self.server.server_port}")

        def respond(self, value, status=200):
            raw = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if not self.allowed_host():
                return self.respond({"error": "Host запрещён"}, 403)
            if self.path == "/":
                raw = html_path.read_text(encoding="utf-8").replace("__CSRF__", json.dumps(csrf)).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.end_headers()
                self.wfile.write(raw)
            elif self.path == "/api/status":
                self.respond(app.status())
            else:
                self.respond({"error": "Не найдено"}, 404)

        def do_POST(self):
            if not self.allowed_host() or not secrets.compare_digest(self.headers.get("X-Joker-Key", ""), csrf):
                return self.respond({"error": "Запрос запрещён"}, 403)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 16000:
                    raise ValueError("Неверный размер запроса.")
                value = json.loads(self.rfile.read(length))
                if self.path == "/api/chat":
                    self.respond(app.handle(value["text"]))
                elif self.path == "/api/mask":
                    self.respond({"reply": app.switch(value["id"])})
                elif self.path == "/api/stop":
                    self.respond({"reply": app.stop()})
                else:
                    self.respond({"error": "Не найдено"}, 404)
            except Cancelled:
                self.respond({"cancelled": True, "reply": "Предыдущий ответ отменён."})
            except ModelError as exc:
                self.respond({"error": str(exc)}, 503)
            except (ValueError, KeyError, TypeError):
                self.respond({"error": "Проверьте поля запроса."}, 400)

    return ThreadingHTTPServer(("127.0.0.1", app.settings.panel_port if port is None else port), Handler)
