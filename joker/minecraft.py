"""Opt-in UnionClef adapter. Game callbacks never call a model on the Java thread."""
import queue
import re
import socket
import threading
import time

from .backends import Cancelled, ModelError
from .game_tasks import advance, TERMINAL, owner_distance

NICK = re.compile(r"[A-Za-z0-9_]{3,16}\Z")
STOP = {"стоп", "хватит", "отмена", "/stop", "/стоп", "подожди здесь", "жди здесь"}
CONTROL = {"/mask", "/маска", "/reset", "/сброс", "/roles", "/роли", "/status", "/статус",
           "/сон", "/sleep", "/проснись", "/wake"}
ITEMS = {"дуб": "oak_log", "дуба": "oak_log", "дубовых брёвен": "oak_log",
         "дубовых бревен": "oak_log", "oak_log": "oak_log", "брёвна": "oak_log",
         "бревна": "oak_log", "булыжник": "cobblestone", "булыжника": "cobblestone",
         "cobblestone": "cobblestone", "землю": "dirt", "земли": "dirt", "dirt": "dirt"}


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
        text = self.strip_address(text).casefold().rstrip(".!?")
        if text in ("/игра", "/minecraft", "что ты делаешь", "что сейчас делаешь"):
            state = self.status()
            return "Minecraft: " + state["detail"] + ". Задача: " + str(state["task"] or "нет")
        if text in STOP:
            return self.app.stop()
        follow = text in ("за мной", "иди за мной", "следуй за мной")
        come = text in ("ко мне", "подойди ко мне", "иди ко мне")
        collect = re.fullmatch(r"(?:принеси|добудь|собери)\s+(.+)", text)
        if not follow and not come and not collect:
            return None
        if self.entry is None:
            return "Minecraft не подключён. Поручение в игру не отправлено."
        if self.task and self.task.get("state") not in TERMINAL:
            return "Сначала скажи «стоп», затем дай новое игровое поручение."
        count, item = 0, ""
        if collect:
            request = re.fullmatch(r"(\d+|два стака|стак)\s+(.+)", collect[1])
            if not request or request[2] not in ITEMS:
                return "Укажи ресурс и количество: например, «принеси 16 дубовых брёвен» или «принеси два стака дубовых брёвен». Сейчас доступны дуб, булыжник и земля."
            count = {"два стака": 128, "стак": 64}.get(request[1], 0)
            if not count:
                count = int(request[1])
            if not 1 <= count <= 256:
                return "Для одной задачи выбери от 1 до 256 предметов."
            item = ITEMS[request[2]]
        try:
            if not self.entry.inGame():
                return "Мир Minecraft закрыт. Поручение не отправлено."
            if self.entry.hasActiveTask():
                return "В Minecraft уже есть активная задача. Сначала скажи «стоп», затем дай новое поручение."
            if follow or come:
                self.entry.ChatMessage(";followPlayer " + self.settings.minecraft_owner)
                task = {"kind": "come" if come else "follow", "target": self.settings.minecraft_owner, "state": "submitted"}
            else:
                self.entry.ExecuteCommand(f"@get {item} {count}")
                task = {"kind": "deliver" if text.startswith("принеси") else "collect", "item": item,
                        "count": count, "target": self.settings.minecraft_owner,
                        "state": "submitted", "phase": "collect", "started_at": time.monotonic()}
            with self.lock:
                self.task = task
            return "Поручение передано в UnionClef. Результат ещё не подтверждён."
        except Exception:
            with self.lock:
                self.task = {"kind": "unknown", "state": "unconfirmed"}
            return "Связь с UnionClef потеряна. Неизвестно, принята ли задача; проверь игру и нажми «стоп»."

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
            return
        if not addressed:
            return
        try:
            self.messages.put_nowait((sender, content, owner, revision, self.epoch, message.get("source", "chat")))
        except queue.Full:
            pass

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
                result = self.app.reply(text, actor="minecraft:" + sender.casefold(), expected_revision=revision)["reply"]
            reply = chat_text(result)
            if source == "voice":
                self.app.voice.metrics["reply_ms"] = round((time.monotonic() - started) * 1000)
            with self.app.guard, self.lock:
                if (reply and self.entry is not None and epoch == self.epoch and not self.shutdown.is_set()
                        and revision == self.app.store.get("response_revision", 0)):
                    self.entry.ChatMessage(reply)
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
    def onDeath(self, killer): pass
    def onKill(self, killed): pass
    def onAutoclefEvent(self, kind, description): pass
    def onDamage(self, amount): pass
    def onDamageConfirmed(self, damaged, attacker, amount): pass
    def onCaptchaSolveRequest(self, image): pass
    def onVoiceFeed(self, player, audio):
        self.service.app.voice.feed(str(player), bytes(audio))
    def agentCommandRequest(self, command): return "not supported"

    class Java:
        implements = ["adris.altoclef.PythonCallback"]
