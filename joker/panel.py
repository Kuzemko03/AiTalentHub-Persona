"""Loopback-only operator panel; no filesystem or shell endpoints."""
import json
import secrets
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .backends import Cancelled, ModelError
from .telegram_bot import TelegramError
from .roles import portrait_path
from .memory import MemoryConflict
from .errands import QueueConflict
from .audio_routes import RouteConflict
from .readiness import report as readiness_report
from .stt import MAX_WAV


class BoundedServer(ThreadingHTTPServer):
    """Bound local sockets as well as generation waiters; controls retain capacity."""
    def __init__(self, *args, **kwargs):
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(*args, **kwargs)

    def process_request(self, request, client_address):
        request.settimeout(10)
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()


def make_server(app, port=None):
    csrf = secrets.token_urlsafe(32)
    chat_slots = threading.BoundedSemaphore(4)
    html_path = app.settings.root / "joker" / "panel.html"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def allowed_host(self):
            return self.headers.get("Host") in (f"127.0.0.1:{self.server.server_port}",
                                                f"localhost:{self.server.server_port}")

        def end_headers(self):
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'nonce-" + csrf
                             + "'; style-src 'unsafe-inline'; img-src 'self'; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            super().end_headers()

        def write_body(self, raw):
            try:
                self.wfile.write(raw)
            except OSError:
                pass  # Closing a browser tab must not produce an uncaught server error.

        def respond(self, value, status=200):
            raw = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.write_body(raw)

        def do_GET(self):
            if not self.allowed_host():
                return self.respond({"error": "Host запрещён"}, 403)
            if self.path == "/":
                raw = (html_path.read_text(encoding="utf-8")
                       .replace("__CSRF__", json.dumps(csrf))
                       .replace("<script>", '<script nonce="' + csrf + '">').encode())
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.write_body(raw)
            elif self.path == "/api/status":
                self.respond(app.status())
            elif self.path in ("/api/audio/devices", "/api/readiness"):
                try:
                    self.respond(app.voice.routes.report() if self.path == "/api/audio/devices" else readiness_report(app))
                except ModelError as exc:
                    self.respond({"error": str(exc)}, 503)
            elif self.path.partition("?")[0] in ("/api/experience", "/api/experience/export"):
                try:
                    route = urllib.parse.urlsplit(self.path)
                    query = urllib.parse.parse_qs(route.query, max_num_fields=1)
                    scope = query.get("scope", ["minecraft"])[0]
                    actor = app.memory.scope(scope)
                    report = {"scope": scope, "at": app.store.now(), "state": app.experience.state(actor),
                              "events": app.experience.recent(actor, 100 if route.path.endswith("export") else 30)}
                    if not route.path.endswith("export"):
                        return self.respond(report)
                    raw = json.dumps(report, ensure_ascii=False, indent=2).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="joker-report-' + scope + '.json"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.write_body(raw)
                except ValueError:
                    self.respond({"error": "Неверная область журнала."}, 400)
            elif self.path.partition("?")[0] in ("/api/memory", "/api/memory/export"):
                try:
                    route = urllib.parse.urlsplit(self.path)
                    query = urllib.parse.parse_qs(route.query, max_num_fields=4)
                    scope = query.get("scope", ["private"])[0]
                    actor = app.memory.scope(scope)
                    if route.path == "/api/memory":
                        return self.respond(app.memory.list(actor))
                    raw = json.dumps(app.memory.export(actor), ensure_ascii=False, indent=2).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Disposition", 'attachment; filename="joker-memory-' + scope + '.json"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.write_body(raw)
                except ValueError:
                    self.respond({"error": "Неверная область памяти."}, 400)
            elif self.path.startswith("/api/role/photo/"):
                path = portrait_path(app.settings.root, self.path.removeprefix("/api/role/photo/"))
                if path is None:
                    return self.respond({"error": "Фотография роли не найдена"}, 404)
                raw = path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.write_body(raw)
            elif self.path.startswith("/api/channel/photo/"):
                try:
                    post_id = self.path.removeprefix("/api/channel/photo/")
                    path = app.channel.photo_path(app.store.channel_post(post_id))
                    raw = path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png" if path.suffix.lower() == ".png" else "image/jpeg")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.write_body(raw)
                except (ValueError, OSError):
                    self.respond({"error": "Изображение не найдено"}, 404)
            else:
                self.respond({"error": "Не найдено"}, 404)

        def do_POST(self):
            key = self.headers.get("X-Joker-Key", "")
            origin = self.headers.get("Origin")
            if (not self.allowed_host() or not key.isascii() or not secrets.compare_digest(key, csrf)
                    or origin is not None and origin not in (f"http://127.0.0.1:{self.server.server_port}",
                                                            f"http://localhost:{self.server.server_port}")):
                return self.respond({"error": "Запрос запрещён"}, 403)
            try:
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("Неподдерживаемый формат передачи.")
                length = int(self.headers.get("Content-Length", "0"))
                if self.path == "/api/stt/preview":
                    if not 44 <= length <= MAX_WAV or self.headers.get("Content-Type") not in ("audio/wav", "audio/x-wav"):
                        raise ValueError("Нужен WAV PCM16 mono до 10 секунд и 1 МБ.")
                    result = app.voice.transcribe_preview(self.rfile.read(length), self.headers.get("X-Joker-Mask"))
                    return self.respond(result)
                if length < 1 or length > 16000:
                    raise ValueError("Неверный размер запроса.")
                value = json.loads(self.rfile.read(length))
                if not isinstance(value, dict):
                    raise ValueError("Ожидается JSON-объект.")
                if self.path == "/api/chat":
                    # Leave sockets available for stop, switching and status requests.
                    if not chat_slots.acquire(blocking=False):
                        return self.respond({"error": "Очередь заполнена. Подождите ответ или нажмите Стоп."}, 429)
                    try:
                        self.respond(app.handle(value["text"]))
                    finally:
                        chat_slots.release()
                elif self.path == "/api/mask":
                    self.respond({"reply": app.switch(value["id"])})
                elif self.path == "/api/stop":
                    self.respond({"reply": app.stop()})
                elif self.path == "/api/game":
                    actions = {"follow": ("follow", {}), "oak": ("deliver", {"item": "oak_log", "count": 16})}
                    action = value.get("action")
                    if action == "stop":
                        return self.respond({"reply": app.stop()})
                    if not isinstance(action, str) or action not in actions:
                        raise ValueError("Неизвестное игровое действие.")
                    tool, arguments = actions[action]
                    observation = app.minecraft.execute(tool, **arguments)
                    self.respond({"reply": observation["detail"], "observation": observation})
                elif self.path == "/api/runtime":
                    actions = {"sleep": "/сон", "wake": "/проснись", "on": "/автономность вкл",
                               "off": "/автономность выкл"}
                    action = value.get("action")
                    if action == "goal":
                        goal = value.get("goal")
                        if not isinstance(goal, str) or not goal.strip() or len(goal) > 1000:
                            raise ValueError("Укажите цель.")
                        reply = app.handle("/цель " + goal.strip())
                    elif isinstance(action, str) and action in actions:
                        reply = app.handle(actions[action])
                    else:
                        raise ValueError("Неизвестное действие.")
                    self.respond(reply)
                elif self.path == "/api/errands":
                    if value.get("action") == "confirm":
                        result = app.errands.confirm(value["id"], value["version"], value["step_id"])
                    else:
                        result = app.errands.control(value["id"], value["version"], value["action"])
                    self.respond({"reply": result["detail"], "errands": result})
                elif self.path == "/api/audio/routes":
                    routes = app.voice.routes.save(value["version"], value["output"], value["input"])
                    self.respond({"reply": "Аудиоустройства сохранены. Голос включается отдельно.", "routes": routes})
                elif self.path == "/api/voice":
                    action = value.get("action")
                    if action == "profile":
                        app.voice.set_voice(value["voice"], mask=value["mask"])
                        return self.respond({"reply": "Голос сохранён для текущей роли."})
                    if action == "on":
                        app.voice.start(mode=value.get("mode"))
                        return self.respond({"reply": app.voice.detail})
                    if action != "off":
                        raise ValueError("Неизвестное действие.")
                    self.respond(app.handle("/голос " + action))
                elif self.path == "/api/voice/preview":
                    raw = app.voice.preview(value["text"], mask=value["mask"])
                    self.send_response(200)
                    self.send_header("Content-Type", "audio/wav")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Disposition", 'inline; filename="joker-voice-preview.wav"')
                    self.end_headers()
                    self.write_body(raw)
                elif self.path == "/api/memory":
                    actor = app.memory.scope(value.get("scope", "private"))
                    action = value.get("action")
                    if action == "list":
                        return self.respond(app.memory.list(actor, value.get("query", ""), value.get("offset", 0)))
                    if action == "read":
                        return self.respond(app.memory.get(actor, value["id"]))
                    if action == "save":
                        note = app.memory.save(actor, title=value["title"], body=value["body"],
                            kind=value.get("kind", "note"), tags=value.get("tags", []), pinned=value.get("pinned", False),
                            note_id=value.get("id"), version=value.get("version"))
                        return self.respond({"reply": "Заметка сохранена.", "note": note})
                    if action == "delete":
                        app.memory.delete(actor, value["id"], value["version"])
                        return self.respond({"reply": "Заметка удалена. История беседы сохраняется."})
                    if action in ("link", "unlink"):
                        note = app.memory.link(actor, value["id"], value["target"], value.get("label", "связано"),
                                               value["version"], remove=action == "unlink")
                        return self.respond({"reply": "Связи обновлены.", "note": note})
                    if action == "forget":
                        removed = app.memory.forget_fact(actor, value["fact"])
                        return self.respond({"reply": "Факт убран из сохранённой памяти. История беседы сохраняется."
                                             if removed else "Такого сохранённого факта нет."})
                    raise ValueError("Неизвестное действие с памятью.")
                elif self.path == "/api/channel/bind":
                    info = app.channel.bind(value["target"])
                    self.respond({"reply": "Канал подключён: " + info["title"]})
                elif self.path == "/api/channel/on":
                    app.channel.enable(True)
                    self.respond({"reply": "Автопостинг включён."})
                elif self.path == "/api/channel/off":
                    app.channel.enable(False)
                    self.respond({"reply": "Автопостинг на паузе."})
                elif self.path == "/api/channel/draft":
                    draft = app.channel.generate(topic=value.get("topic", ""), auto_role=value.get("auto_role") is True)
                    self.respond({"reply": "Черновик готов: " + draft["title"]})
                elif self.path in ("/api/comments/on", "/api/comments/off"):
                    app.comments.enable(self.path.endswith("/on"))
                    self.respond({"reply": "Ответы в комментариях " + ("включены" if self.path.endswith("/on") else "выключены")})
                elif self.path == "/api/channel/publish":
                    published = app.channel.publish(value["id"], automatic=False)
                    self.respond({"reply": "Опубликовано: " + published["title"]})
                elif self.path == "/api/channel/discard":
                    app.channel.discard(value["id"])
                    self.respond({"reply": "Черновик закрыт."})
                else:
                    self.respond({"error": "Не найдено"}, 404)
            except Cancelled:
                self.respond({"cancelled": True, "reply": "Предыдущий ответ отменён."})
            except (MemoryConflict, QueueConflict, RouteConflict) as exc:
                self.respond({"error": str(exc)}, 409)
            except ModelError as exc:
                self.respond({"error": str(exc)}, 503)
            except TelegramError as exc:
                self.respond({"error": str(exc)}, 503)
            except (ValueError, KeyError, TypeError, OSError, RecursionError):
                self.respond({"error": "Проверьте поля запроса."}, 400)

    return BoundedServer(("127.0.0.1", app.settings.panel_port if port is None else port), Handler)
