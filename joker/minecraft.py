"""Opt-in UnionClef adapter. Game callbacks never call a model on the Java thread."""
import json
import math
import queue
import re
import socket
import threading
import time

from .backends import Cancelled, ModelError
from .game_tasks import advance, owner_distance, inventory

NICK = re.compile(r"[A-Za-z0-9_]{3,16}\Z")
STOP = {"стоп", "/stop", "/стоп"}
CONTROL = {"/mask", "/маска", "/reset", "/сброс", "/roles", "/роли", "/status", "/статус",
           "/сон", "/sleep", "/проснись", "/wake", "/заметка", "/note", "/заметки", "/notes",
           "/найди", "/find", "/забудь", "/forget", "/связь", "/link"}



def probe(settings):
    """Read-only TCP check; never imports Py4J or starts Minecraft."""
    try:
        with socket.create_connection(("127.0.0.1", settings.minecraft_port), timeout=.3):
            available = True
    except OSError:
        available = False
    return {"port": settings.minecraft_port, "port_available": available,
            "owner_configured": bool(NICK.fullmatch(settings.minecraft_owner)),
            "bot_configured": bool(NICK.fullmatch(settings.minecraft_bot))}


def chat_text(text):
    """A model answer is chat, never a server, AltoClef or Tungsten command."""
    text = " ".join(str(text).split())
    text = "".join(c for c in text if c.isprintable() and c != "§")
    return text.lstrip(" /@;!$\\")[:240]


class MinecraftService:
    def __init__(self, app):
        self.app = app
        self.settings = app.settings
        self.shutdown = threading.Event()
        self.lock = threading.RLock()
        self.messages = queue.Queue(maxsize=16)
        self.entry = None
        self.gateway = None
        self.detail = "не подключён"
        self.task = None
        self.busy = False
        self.epoch = 0
        self.thread = None
        self.worker_thread = None
        self.next_join = 0
        self._last_task_event = None
        self._callback_times = {}
        self.resources = json.loads((app.home.parent / "shared" / "minecraft-items.json").read_text(encoding="utf-8"))
        if (not isinstance(self.resources, dict) or not self.resources or len(self.resources) > 128
                or any(not re.fullmatch(r"[a-z0-9_]{1,80}", k) or not isinstance(v, str)
                       or not 1 <= len(v) <= 200 for k, v in self.resources.items())):
            raise ValueError("Неверный каталог игровых ресурсов.")
        if app.store.get("pending_game_action"):
            self.task = {"kind": "restored", "state": "unconfirmed",
                         "detail": "После перезапуска проверь игру и останови прежнюю задачу."}

    def status(self):
        with self.lock:
            return {"connected": self.entry is not None, "detail": self.detail,
                    "busy": self.busy, "task": dict(self.task) if self.task else None,
                    "queued": self.messages.qsize()}

    def connect(self):
        if not NICK.fullmatch(self.settings.minecraft_owner) or not NICK.fullmatch(self.settings.minecraft_bot):
            raise ValueError("Задайте MINECRAFT_OWNER и MINECRAFT_BOT: игровые ники из 3–16 латинских букв, цифр или _." )
        if self.settings.minecraft_owner.casefold() == self.settings.minecraft_bot.casefold():
            raise ValueError("Ники владельца и Джокера должны различаться.")
        try:
            from py4j.java_gateway import JavaGateway, GatewayParameters, CallbackServerParameters
        except ImportError:
            raise ValueError('Для Minecraft установите дополнительный пакет: pip install -e ".[minecraft]"') from None
        gateway = JavaGateway(
            gateway_parameters=GatewayParameters(address="127.0.0.1", port=self.settings.minecraft_port,
                                                 auto_convert=True, read_timeout=3),
            callback_server_parameters=CallbackServerParameters(address="127.0.0.1",
                port=self.settings.minecraft_callback_port, daemonize=True, daemonize_connections=True),
            python_server_entry_point=GameCallbacks(self))
        try:
            if str(gateway.entry_point.getUsername()).casefold() != self.settings.minecraft_bot.casefold():
                raise ValueError("Ник игрового клиента не совпадает с MINECRAFT_BOT.")
            if (not gateway.entry_point.inGame() and self.settings.minecraft_autoconnect
                    and not self.app.runtime.sleeping() and time.monotonic() >= self.next_join):
                server = self.settings.minecraft_server
                if (not re.fullmatch(r"[A-Za-z0-9.-]+(?::\d{1,5})?", server)
                        or ":" in server and not 1 <= int(server.rsplit(":", 1)[1]) <= 65535):
                    raise ValueError("Задайте MINECRAFT_SERVER: имя сервера и необязательный порт.")
                self.next_join = time.monotonic() + 60
                gateway.entry_point.ConnectToServer(server)
            self.attach(gateway.entry_point)
            address = gateway.jvm.java.net.InetAddress.getByName("127.0.0.1")
            gateway.java_gateway_server.resetCallbackClient(address, self.settings.minecraft_callback_port)
            gateway.entry_point.InitPythonCallback()
            if not gateway.entry_point.IsCallbackServerStarted():
                raise ValueError("UnionClef не подтвердил обратное соединение Python.")
            if self.shutdown.is_set():
                raise ValueError("Подключение отменено при завершении приложения.")
        except Exception:
            self.detach()
            gateway.shutdown_callback_server()
            gateway.close()
            raise
        self.gateway = gateway

    def attach(self, entry):
        """Validate the actual client identity before admitting events or actions."""
        if not entry.inGame():
            raise ValueError("Откройте игровой мир в Minecraft с UnionClef.")
        if str(entry.getUsername()).casefold() != self.settings.minecraft_bot.casefold():
            raise ValueError("Ник игрового клиента не совпадает с MINECRAFT_BOT.")
        with self.lock:
            self.entry = entry
            self.epoch += 1
            self.detail = "подключён через UnionClef"

    def detach(self):
        with self.lock:
            self.entry = None
            self.epoch += 1
            self.busy = False
            self.task = None  # Never replay a task after reconnect or restart.
            self.detail = "нет связи с UnionClef"
            while not self.messages.empty():
                try:
                    self.messages.get_nowait()
                except queue.Empty:
                    break

    def refresh(self):
        with self.app.guard, self.lock:
            entry = self.entry
            if entry is None or not entry.inGame():
                raise ValueError("Minecraft не находится в игровом мире.")
            busy = bool(entry.hasActiveTask())
            self.busy = busy
            if self.task and self.task["kind"] in ("collect", "deliver"):
                advance(self, busy)
            elif self.task and self.task["kind"] == "come":
                if self.task.get("phase") == "stopping":
                    if not busy:
                        self.task.update(state="arrived", detail="Владелец в пределах трёх блоков, движение остановлено.")
                elif owner_distance(entry, self.settings.minecraft_owner) <= 3:
                    entry.stopPathing()
                    self.task.update(phase="stopping", state="running")
                elif busy:
                    self.task["state"] = "running"
            elif self.task:
                if busy:
                    self.task["state"] = "running"
                elif self.task["state"] == "running":
                    # Idle does not establish that the requested resources were collected.
                    self.task["state"] = "idle_unverified"
            self.trace_task()

    def stop_task(self):
        with self.app.guard, self.lock:
            return self._stop_task()

    def _stop_task(self):
        entry = self.entry
        if entry is None:
            return "Игровое управление пока не подключено."
        with self.lock:
            self.task = {"kind": "stop", "state": "unconfirmed"}
        try:
            result = entry.stopPathing()
            if not result or not result.get("ok"):
                return "UnionClef не подтвердил приём остановки."
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                if entry.inGame() and not entry.hasActiveTask():
                    with self.lock:
                        self.busy = False
                        self.task = None
                        self.trace_task()
                    return "UnionClef подтвердил: активной игровой задачи нет."
                self.shutdown.wait(.1)
            return "Остановка передана в UnionClef; завершение задачи ещё не подтверждено."
        except Exception:
            return "Связь с UnionClef потеряна; остановка игры не подтверждена."

    def strip_address(self, text):
        with self.app.guard:
            name = self.app.masks[self.app.active]["display_name"]
        for alias in ("Джокер", self.settings.minecraft_bot, name):
            text = re.sub(r"^\s*" + re.escape(alias) + r"\b[\s,:!—-]*", "", text, flags=re.I)
        return text.strip()

    def command(self, text):
        # Explicit operator controls only. Ordinary language belongs to the model.
        text = self.strip_address(text).casefold().rstrip(".!?")
        if text in ("/игра", "/minecraft"):
            state = self.status()
            return "Minecraft: " + state["detail"] + ". Задача: " + str(state["task"] or "нет")
        if text in STOP:
            return self.app.stop()
        return None

    def observe(self):
        result = self.status()
        if self.entry is None:
            return result
        try:
            result["inventory"] = inventory(self.entry)
            players = []
            for raw in self.entry.getPlayersInfo(64):
                name = str(raw.get("name", ""))
                distance = float(raw.get("distance", "nan"))
                if NICK.fullmatch(name) and math.isfinite(distance) and distance >= 0:
                    players.append({"name": name, "distance": round(distance, 2)})
            result["nearby_players"] = players[:64]
        except Exception:
            result["observation_error"] = "Не все игровые сведения доступны."
        return result

    def trace_task(self):
        task = {k: v for k, v in (self.task or {}).items() if k != "started_at"}
        if task != self._last_task_event:
            self._last_task_event = dict(task)
            self.app.experience.record("world:minecraft", "minecraft", "task.observed", task)
        if (not task or task.get("state") in ("collected", "arrived")) and not self.busy:
            self.app.store.set("pending_game_action", None)

    def execute(self, action, item=None, count=None):
        """Typed bridge operation. No user text, aliases or behavior selection."""
        if action not in ("follow", "come", "collect", "deliver"):
            raise ValueError("Неизвестный игровой инструмент.")
        if action in ("collect", "deliver") and (not isinstance(item, str) or item not in self.resources
                or type(count) is not int or not 1 <= count <= 256):
            raise ValueError("Нужен ресурс из каталога и количество от 1 до 256.")
        with self.app.guard, self.lock:
            if self.entry is None:
                return {"accepted": False, "completed": False, "detail": "Minecraft не подключён. Поручение не отправлено."}
            if self.task and self.task.get("state") not in ("collected", "arrived"):
                return {"accepted": False, "completed": False,
                        "detail": "Сначала проверь прежнее поручение и нажми «стоп».", "task": dict(self.task)}
            try:
                if not self.entry.inGame() or self.entry.hasActiveTask():
                    return {"accepted": False, "completed": False,
                            "detail": "Мир закрыт или есть активная задача. Проверь игру и нажми «стоп»; новое поручение не отправлено."}
                task = {"kind": action, "target": self.settings.minecraft_owner, "state": "submitted"}
                if action in ("collect", "deliver"):
                    task.update(item=item, count=count, phase="collect", started_at=time.monotonic())
                # Write intent before any bridge side effect; never replay it after restart.
                self.app.store.set("pending_game_action", {k: v for k, v in task.items() if k != "started_at"})
                self.task = task
                if action in ("follow", "come"):
                    self.entry.ChatMessage(";followPlayer " + self.settings.minecraft_owner)
                else:
                    self.entry.ExecuteCommand(f"@get {item} {count}")
                self.trace_task()
                return {"accepted": True, "completed": False, "task": dict(task),
                        "detail": "Поручение передано в UnionClef. Результат ещё не подтверждён."}
            except Exception:
                self.task = {"kind": "unknown", "state": "unconfirmed"}
                self.trace_task()
                return {"accepted": None, "completed": False, "task": dict(self.task),
                        "detail": "Связь потеряна; приём задачи неизвестен. Проверь игру и останови задачу."}

    def event(self, kind, data):
        if self.entry is None or self.shutdown.is_set():
            return
        # Bound noisy callbacks, without choosing a character's reaction.
        now = time.monotonic()
        previous = self._callback_times.get(kind, 0)
        if now - previous < 1:
            return
        self._callback_times[kind] = now
        self.app.experience.record("world:minecraft", "minecraft", kind, data)

    def accept(self, message):
        """Only typed/parsed chat; never infer the sender from a raw chat string."""
        if self.entry is None or self.shutdown.is_set():
            return
        sender, text = message.get("user"), message.get("msg")
        if (message.get("parse_type") == "unparsed" or not isinstance(sender, str)
                or not NICK.fullmatch(sender) or not isinstance(text, str) or not 0 < len(text) <= 1000
                or sender.casefold() == self.settings.minecraft_bot.casefold()):
            return
        with self.app.guard:
            aliases = ("джокер", self.settings.minecraft_bot, self.app.masks[self.app.active]["display_name"])
            revision = self.app.store.get("response_revision", 0)
        addressed = any(re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", text, re.I) for a in aliases)
        owner = sender.casefold() == self.settings.minecraft_owner.casefold()
        content = self.strip_address(text)
        if owner and content.casefold().rstrip(".!?") in STOP:
            reply = self.app.stop()  # Priority path: no queue and no model request.
            self.control_reply(reply)
            return
        if owner and content.casefold() in ("/проснись", "/wake"):
            self.control_reply(self.app.runtime.wake())
            return
        if self.app.runtime.sleeping():
            if addressed:
                self.event("chat.ignored.sleep", {"actor": sender})
            return
        if not addressed:
            return
        try:
            self.messages.put_nowait((sender, content, owner, revision, self.epoch, message.get("source", "chat")))
            self.event("chat.queued", {"actor": sender, "source": message.get("source", "chat"), "text": content[:500]})
        except queue.Full:
            self.event("chat.dropped.queue_full", {"actor": sender})

    def control_reply(self, reply):
        with self.app.guard, self.lock:
            if self.entry is not None and not self.shutdown.is_set():
                try:
                    self.entry.ChatMessage(chat_text(reply))
                except Exception:
                    pass  # A failed chat acknowledgement must not undo the stop itself.

    def work_once(self):
        try:
            started = time.monotonic()
            sender, text, owner, revision, epoch, source = self.messages.get_nowait()
        except queue.Empty:
            return False
        token = text.partition(" ")[0].casefold()
        if token.startswith("/") and token not in CONTROL:
            return True  # No Telegram/editorial/private-memory commands in a public game.
        try:
            with self.app.guard:
                if (revision != self.app.store.get("response_revision", 0)
                        or self.entry is None or epoch != self.epoch):
                    return True
            if owner:
                result = self.app.command(text, actor="minecraft:" + sender.casefold(), expected_revision=revision)
                # Switching a mask legitimately changes the response revision.
                revision = self.app.store.get("response_revision", 0)
            else:
                # Guests may converse, but cannot mutate the shared role or run tools.
                from .roles import role_request
                if token in CONTROL or role_request(text):
                    return True
                result = None
            if result is None:
                answer = self.app.reply(text, actor="minecraft:" + sender.casefold(),
                                        expected_revision=revision, owner=owner)
                result, revision = answer["reply"], answer["_revision"]
            reply = chat_text(result)
            if source == "voice":
                self.app.voice.metrics["reply_ms"] = round((time.monotonic() - started) * 1000)
            with self.app.guard, self.lock:
                if (reply and self.entry is not None and epoch == self.epoch and not self.shutdown.is_set()
                        and revision == self.app.store.get("response_revision", 0)):
                    self.entry.ChatMessage(reply)
                    self.event("reply.submitted", {"actor": sender, "chars": len(reply), "source": source})
                    if source == "voice":
                        self.app.voice.enqueue(reply, revision, epoch)
            return True
        except (Cancelled, ModelError, ValueError):
            return True

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        if not NICK.fullmatch(self.settings.minecraft_owner) or not NICK.fullmatch(self.settings.minecraft_bot):
            raise ValueError("Перед запуском Minecraft задайте MINECRAFT_OWNER и MINECRAFT_BOT в .env.")
        if self.settings.minecraft_owner.casefold() == self.settings.minecraft_bot.casefold():
            raise ValueError("Ники владельца и Джокера должны различаться.")
        if not all(1 <= p <= 65535 for p in (self.settings.minecraft_port, self.settings.minecraft_callback_port)):
            raise ValueError("Порты Minecraft должны быть в пределах 1–65535.")
        if self.settings.minecraft_port == self.settings.minecraft_callback_port:
            raise ValueError("Порты Java и Python callback должны различаться.")
        import importlib.util
        if importlib.util.find_spec("py4j") is None:
            raise ValueError('Установите адаптер: pip install -e ".[minecraft]"')
        self.shutdown.clear()
        self.thread = threading.Thread(target=self.run, daemon=True, name="joker-minecraft")
        self.thread.start()

    def run(self):
        self.worker_thread = threading.Thread(target=self.worker, daemon=True, name="joker-minecraft-chat")
        self.worker_thread.start()
        while not self.shutdown.is_set():
            try:
                if self.entry is None:
                    self.connect()
                self.refresh()
                self.shutdown.wait(1)
            except Exception as exc:
                self.disconnect()
                self.detail = str(exc) if isinstance(exc, ValueError) else "ожидаю Minecraft с UnionClef"
                self.shutdown.wait(5)

    def worker(self):
        while not self.shutdown.is_set():
            try:
                if self.work_once():
                    self.shutdown.wait(3)  # Bounded reply frequency; callback stays responsive.
                else:
                    self.shutdown.wait(.1)
            except Exception:
                self.detail = "ошибка игрового обработчика; проверь подключение"
                self.shutdown.wait(1)

    def disconnect(self):
        self.detach()
        if self.gateway:
            try:
                self.gateway.shutdown_callback_server()
                self.gateway.close()  # Never gateway.shutdown(): that would stop the Java server.
            except Exception:
                pass
            self.gateway = None

    def close(self):
        self.shutdown.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=4)
        self.disconnect()


class GameCallbacks:
    """UnionClef v0.95.61 adris.altoclef.PythonCallback interface."""
    def __init__(self, service):
        self.service = service

    def isStarted(self):
        return not self.service.shutdown.is_set()

    def onVerifedChat(self, message=None):
        if message is not None:
            self.service.accept({str(k): str(v) for k, v in message.items()})
        return message

    def onChatMessage(self, text): return text
    def onUpdateServerInfo(self, info): return info
    def onDeath(self, killer): self.service.event("death", {"killer": str(killer)[:120]})
    def onKill(self, killed): self.service.event("kill", {"target": str(killed)[:120]})
    def onAutoclefEvent(self, kind, description):
        self.service.event("autoclef", {"kind": str(kind)[:80], "description": str(description)[:500]})
    def onDamage(self, amount): self.service.event("damage", {"amount": str(amount)[:32]})
    def onDamageConfirmed(self, damaged, attacker, amount):
        self.service.event("damage.confirmed", {"target": str(damaged)[:120], "attacker": str(attacker)[:120], "amount": str(amount)[:32]})
    def onCaptchaSolveRequest(self, image): pass
    def onVoiceFeed(self, player, audio):
        self.service.app.voice.feed(str(player), bytes(audio))
    def agentCommandRequest(self, command): return "not supported"

    class Java:
        implements = ["adris.altoclef.PythonCallback"]
