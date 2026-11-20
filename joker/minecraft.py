"""Opt-in UnionClef adapter. Game callbacks never call a model on the Java thread."""
import json
import math
import queue
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, replace

from .backends import Cancelled, ModelError
from .game_tasks import advance, owner_distance, inventory, visible_player
from . import navigation

NICK = re.compile(r"[A-Za-z0-9_]{3,16}\Z")
STOP = {"стоп", "/stop", "/стоп"}
CONTROL = {"/mask", "/маска", "/reset", "/сброс", "/roles", "/роли", "/status", "/статус",
           "/сон", "/sleep", "/проснись", "/wake", "/заметка", "/note", "/заметки", "/notes",
           "/найди", "/find", "/забудь", "/forget", "/связь", "/link", "/поручения", "/queue", "/голос", "/voice"}


@dataclass(frozen=True)
class GameTurn:
    sender: str
    source: str
    epoch: int
    task_id: str | None = None

    @property
    def actor(self):
        return "minecraft:" + self.sender.casefold()


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


def chat_chunks(text):
    """Split normal replies on word boundaries; never send an executable prefix."""
    normalized = " ".join(str(text).split())
    words = "".join(c for c in normalized if c.isprintable() and c != "§").split()
    chunks, line = [], ""
    for word in words:
        if len(word.encode("utf-16-le")) // 2 > 240:
            raise ValueError("В ответе есть слишком длинное слово; нужен более короткий ответ.")
        candidate = line + (" " if line else "") + word
        if len(candidate.encode("utf-16-le")) // 2 > 240:
            chunks.append(line.lstrip(" /@;!$\\"))
            line = word
        else:
            line = candidate
    if line:
        chunks.append(line.lstrip(" /@;!$\\"))
    if len(chunks) > 4:
        raise ValueError("Ответ слишком длинный для игрового чата; нужен ответ до четырёх сообщений.")
    return [line for line in chunks if line]


class MinecraftService:
    def __init__(self, app):
        self.app = app
        self.settings = app.settings
        self.shutdown = threading.Event()
        self.lock = threading.RLock()
        self.messages = queue.Queue(maxsize=16)
        self.handling_message = False
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
                    "queued": self.messages.qsize(), "resources": dict(self.resources)}

    def connect(self):
        if not NICK.fullmatch(self.settings.minecraft_owner) or not NICK.fullmatch(self.settings.minecraft_bot):
            raise ValueError("Задайте MINECRAFT_OWNER и MINECRAFT_BOT: игровые ники из 3–16 латинских букв, цифр или _." )
        if self.settings.minecraft_owner.casefold() == self.settings.minecraft_bot.casefold():
            raise ValueError("Ники владельца и Джокера должны различаться.")
        try:
            from py4j.java_gateway import JavaGateway, GatewayParameters, CallbackServerParameters
        except ImportError:
            raise ValueError('Для Minecraft установите дополнительный пакет: pip install -e ".[minecraft]"') from None
        callbacks = GameCallbacks(self)
        gateway = JavaGateway(
            gateway_parameters=GatewayParameters(address="127.0.0.1", port=self.settings.minecraft_port,
                                                 auto_convert=True, read_timeout=3),
            callback_server_parameters=CallbackServerParameters(address="127.0.0.1",
                port=self.settings.minecraft_callback_port, daemonize=True, daemonize_connections=True),
            python_server_entry_point=callbacks)
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
            callbacks.epoch = self.attach(gateway.entry_point)
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
            return self.epoch

    def detach(self):
        with self.app.guard, self.lock:
            if hasattr(self.app, "errands"):
                self.app.errands.pause()
            self.entry = None
            self.epoch += 1
            self.busy = False
            self.task = None  # Never replay a task after reconnect or restart.
            self.detail = "нет связи с UnionClef"
            self.discard_messages()

    def discard_messages(self):
        while True:
            try:
                self.messages.get_nowait()
            except queue.Empty:
                return

    def dialogue_pending(self):
        return self.handling_message or not self.messages.empty()

    def refresh(self):
        with self.app.guard, self.lock:
            entry = self.entry
            if entry is None or not entry.inGame():
                raise ValueError("Minecraft не находится в игровом мире.")
            busy = navigation.active(entry)
            self.busy = busy
            if self.task and self.task["kind"] in ("collect", "deliver"):
                advance(self, busy)
            elif self.task and self.task["kind"] == "walk":
                navigation.refresh(self, busy)
            elif self.task and self.task["kind"] == "approach":
                navigation.refresh_approach(self, busy)
            elif self.task and self.task["kind"] == "come":
                target = self.task["target"]
                distance = owner_distance(entry, target)
                if self.task.get("state") in ("failed", "unconfirmed", "arrived"):
                    pass
                elif time.monotonic() - self.task.get("started_at", time.monotonic()) > 900:
                    entry.stopPathing()
                    self.task.update(state="failed", detail="Истекли 15 минут ожидания подхода. Проверь игру.")
                elif self.task.get("phase") == "stopping":
                    if not busy:
                        if distance <= 3:
                            self.task.update(state="arrived", detail=f"{target} в пределах трёх блоков, движение остановлено.")
                        else:
                            self.task.update(state="unconfirmed",
                                             detail="Игрок отошёл; движение остановлено, прибытие не подтверждено.")
                elif distance <= 3:
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

    def release_task(self):
        with self.app.guard, self.lock:
            if self.task and self.task.get("kind") not in ("follow", "come", "approach", "walk", "stop"):
                return {"accepted": False, "completed": False,
                        "detail": "Состояние ресурсного поручения требует проверки владельцем. Маршрут не заменён."}
            detail = self._stop_task()
            stopped = self.entry is not None and self.task is None
            return {"accepted": stopped, "completed": stopped, "detail": detail}

    def _stop_task(self):
        entry = self.entry
        if entry is None:
            return "Игровое управление пока не подключено."
        previous = (self.task or {}).get("kind")
        with self.lock:
            self.task = {"kind": "stop", "state": "unconfirmed"}
        try:
            result = entry.stopPathing()
            if not result or not result.get("ok"):
                return "UnionClef не подтвердил приём остановки."
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if (entry.inGame() and not navigation.active(entry)
                        and navigation.stationary(entry, self.settings.minecraft_bot)):
                    with self.lock:
                        self.busy = False
                        self.task = None
                        pending = self.app.runtime.state.get("pending_action")
                        if pending and pending.get("action") == previous:
                            self.app.runtime.state.pop("pending_action")
                            self.app.runtime.save()
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
        entry = self.entry
        if entry is None:
            return result
        try:
            result["inventory"] = inventory(entry)
        except Exception:
            result["observation_error"] = "Не все игровые сведения доступны."
        try:
            players = []
            for raw in entry.getPlayersInfo(64):
                name = str(raw.get("name", ""))
                distance = float(raw.get("distance", "nan"))
                if NICK.fullmatch(name) and math.isfinite(distance) and distance >= 0:
                    player = {"name": name, "distance": round(distance, 2)}
                    position = navigation.parse_position(str(raw.get("position", "")).split(","))
                    if position is not None:
                        player["position"] = position
                    for key in ("hand_item", "ground_block", "gamemode"):
                        value = str(raw.get(key, ""))
                        if value:
                            player[key] = value[:100]
                    players.append(player)
            result["nearby_players"] = players[:64]
            result["self"] = next((p for p in players if p["name"].casefold() == self.settings.minecraft_bot.casefold()), None)
        except Exception:
            result["observation_error"] = "Не все игровые сведения доступны."
        try:
            coordinates = entry.targetKinematics(self.settings.minecraft_bot)
            position = navigation.parse_position([coordinates[k] for k in ("x", "y", "z")])
            if position is not None:
                result["self"] = {**(result.get("self") or {"name": self.settings.minecraft_bot}), "position": position}
        except Exception:
            pass  # Older bridges can still supply the rounded position in the player list.
        try:
            result["surface_map"] = navigation.surface(entry, result.get("self"))
        except Exception:
            result["surface_map"] = []
        return result

    def trace_task(self):
        task = {k: v for k, v in (self.task or {}).items()
                if k not in ("started_at", "last_progress_at", "last_position", "best_distance", "stop_requested_at")}
        if task != self._last_task_event:
            self._last_task_event = dict(task)
            self.app.experience.record("world:minecraft", "minecraft", "task.observed", task)
            if task.get("state") in ("arrived", "collected", "not_arrived") and hasattr(self.app, "runtime"):
                self.app.runtime.next_tick = 0  # A completed leg makes the model lane ready again.
                if self.app.runtime.state.get("pending_action", {}).get("action") == task.get("kind"):
                    self.app.runtime.state.pop("pending_action", None)
                    self.app.runtime.save()
                if task.get("source") and not self.shutdown.is_set():
                    turn = GameTurn(task["target"], task["source"], self.epoch, task["id"])
                    try:
                        self.messages.put_nowait((turn, "", self.app.store.get("response_revision", 0), None))
                    except queue.Full:
                        self.event("chat.dropped.queue_full", {"actor": turn.sender})
        if (not task or task.get("state") in ("collected", "arrived", "not_arrived")) and not self.busy:
            self.app.store.set("pending_game_action", None)
        if hasattr(self.app, "errands"):
            self.app.errands.observe(task, self.busy)

    def say(self, message):
        """Bounded public chat transport. Model-written content, durable no-replay receipt."""
        if not isinstance(message, str):
            raise ValueError("Нужен текст игрового сообщения.")
        message = " ".join(message.split())
        message = "".join(c for c in message if c.isprintable() and c != "§")
        if (not message or message[0] in "/@;!$\\" or len(message.encode("utf-16-le")) // 2 > 240):
            raise ValueError("Нужна обычная реплика до 240 символов, без команд.")
        with self.app.guard, self.lock:
            if self.entry is None:
                return {"accepted": False, "completed": False, "detail": "Minecraft не подключён. Сообщение не отправлено."}
            now = time.time()
            outbox = self.app.store.get("game_chat_outbox", [])
            if any(p["message"] == message and (p["state"] != "queued" or now - p["at"] < 300) for p in outbox):
                return {"accepted": False, "completed": False, "detail": "Эта реплика уже отправлялась. Автоматический повтор не выполнен."}
            if outbox and now - outbox[-1]["at"] < 15:
                return {"accepted": False, "completed": False, "detail": "Пауза между инициативными сообщениями — 15 секунд. Сообщение не отправлено."}
            if not self.entry.inGame():
                return {"accepted": False, "completed": False, "detail": "Игровой мир закрыт. Сообщение не отправлено."}
            receipt = {"id": uuid.uuid4().hex, "at": now, "message": message, "state": "pending"}
            outbox = (outbox + [receipt])[-30:]
            self.app.store.set("game_chat_outbox", outbox)
            try:
                self.entry.ChatMessage(message)
            except Exception:
                receipt["state"] = "uncertain"
                self.app.store.set("game_chat_outbox", outbox)
                return {"accepted": None, "completed": False, "detail": "Приём сообщения неизвестен. Повтор не выполняется; проверь игровой чат."}
            receipt["state"] = "queued"
            self.app.store.set("game_chat_outbox", outbox)
            self.app.experience.record("world:minecraft", "minecraft", "chat.submitted", {"message": message})
            self.app.voice.enqueue(message, self.app.store.get("response_revision", 0), self.epoch)
            return {"accepted": True, "completed": False, "message": message,
                    "detail": "Реплика передана в очередь игрового чата. Доставка сервером ещё не подтверждена."}

    def execute(self, action, item=None, count=None, *, player=None, x=None, y=None, z=None, task_id=None,
                game_turn=None, autonomous=False):
        """Typed bridge operation. No user text, aliases or behavior selection."""
        if action not in ("follow", "come", "collect", "deliver", "approach", "walk"):
            raise ValueError("Неизвестный игровой инструмент.")
        if action in ("collect", "deliver") and (not isinstance(item, str) or item not in self.resources
                or type(count) is not int or not 1 <= count <= 256):
            raise ValueError("Нужен ресурс из каталога и количество от 1 до 256.")
        if action == "approach" and (not isinstance(player, str) or not NICK.fullmatch(player)):
            raise ValueError("Нужен точный игровой ник выбранного игрока.")
        if action == "walk":
            navigation.validate_point(x, y, z)
        with self.app.guard, self.lock:
            queue = getattr(self.app, "errands", None)
            if queue and queue.owns_game() and (not task_id or task_id != queue.current()["task_id"]
                    or queue.current()["status"] != "dispatching"):
                return {"accepted": False, "completed": False,
                        "detail": "Сначала приостанови и отмени список поручений. Отдельное действие не отправлено."}
            if self.entry is None:
                return {"accepted": False, "completed": False, "detail": "Minecraft не подключён. Поручение не отправлено."}
            if game_turn is not None and (action != "approach" or game_turn.epoch != self.epoch
                                          or player.casefold() != game_turn.sender.casefold()):
                raise Cancelled("Подход относится к другому игровому событию.")
            task_before = self.task
            try:
                target = self.settings.minecraft_owner
                if action == "approach":
                    visible = visible_player(self.entry, player, navigation.APPROACH_RANGE)
                    if visible is None or player.casefold() == self.settings.minecraft_bot.casefold():
                        return {"accepted": False, "completed": False,
                                "detail": f"Для подхода нужен игрок {player} в пределах видимости."}
                    target, distance = visible
                if self.task and self.task.get("state") not in ("collected", "arrived", "not_arrived"):
                    if (game_turn is None or not self.task.get("autonomous")
                            or self.task["kind"] not in ("walk", "approach")
                            or self.task["state"] not in ("submitted", "running")):
                        return {"accepted": False, "completed": False, "detail": "Сначала нужно закончить текущее поручение или нажать «стоп».",
                                "task": dict(self.task)}
                    stopped = self.release_task()
                    if not stopped["completed"]:
                        return stopped
                    task_before = self.task
                    visible = visible_player(self.entry, target, navigation.APPROACH_RANGE)
                    if visible is None:
                        return {"accepted": False, "completed": False,
                                "detail": f"Игрок {target} ушёл из видимости во время остановки."}
                    target, distance = visible
                if not self.entry.inGame() or navigation.active(self.entry):
                    return {"accepted": False, "completed": False,
                            "detail": "Мир закрыт или есть активная задача. Проверь игру и нажми «стоп»."}
                if action == "walk":
                    return navigation.start(self, x, y, z, task_id, autonomous=autonomous)
                task = {"id": task_id or uuid.uuid4().hex, "kind": action,
                        "target": target, "state": "submitted"}
                if action in ("come", "approach"):
                    task["started_at"] = time.monotonic()
                if action == "approach":
                    task.update(best_distance=distance, last_progress_at=task["started_at"])
                    if game_turn is not None:
                        task["source"] = game_turn.source
                    if autonomous:
                        task["autonomous"] = True
                if action in ("collect", "deliver"):
                    task.update(item=item, count=count, phase="collect", started_at=time.monotonic())
                # Write intent before any bridge side effect; never replay it after restart.
                self.app.store.set("pending_game_action", {k: v for k, v in task.items() if k != "started_at"})
                self.task = task
                if action in ("follow", "come", "approach"):
                    self.entry.ChatMessage(";followPlayer " + target)
                else:
                    self.entry.ExecuteCommand(f"@get {item} {count}")
                self.trace_task()
                return {"accepted": True, "completed": False, "task": dict(task),
                        "detail": "Поручение передано в UnionClef. Результат ещё не подтверждён."}
            except Exception:
                if self.task is task_before:
                    return {"accepted": False, "completed": False,
                            "detail": "Не удалось получить игровое состояние. Движение не начато."}
                self.task = {"id": task_id or (self.task or {}).get("id", ""), "kind": "unknown", "state": "unconfirmed"}
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

    def accept(self, message, *, voice_ticket=None, expected_epoch=None):
        """Only typed/parsed chat; never infer the sender from a raw chat string."""
        if self.entry is None or self.shutdown.is_set():
            return
        sender, text = message.get("user"), message.get("msg")
        if (message.get("parse_type") != "parsed" or not isinstance(sender, str)
                or not NICK.fullmatch(sender) or not isinstance(text, str) or not 0 < len(text) <= 1000
                or sender.casefold() == self.settings.minecraft_bot.casefold()):
            return
        with self.app.guard:
            if (self.entry is None or expected_epoch is not None and expected_epoch != self.epoch
                    or voice_ticket is not None and not self.app.voice.current(voice_ticket)):
                return
            aliases = ("джокер", self.settings.minecraft_bot, self.app.masks[self.app.active]["display_name"])
            revision = self.app.store.get("response_revision", 0)
            addressed = any(re.search(r"(?<!\w)" + re.escape(a) + r"(?!\w)", text, re.I) for a in aliases)
            owner = sender.casefold() == self.settings.minecraft_owner.casefold()
            content = self.strip_address(text) or text
            if owner and content.casefold().rstrip(".!?") in STOP:
                reply = self.app.stop()  # Priority path: no queue and no model request.
                # Players see a short line; the full diagnostic stays in the panel and logs.
                self.control_reply("Стою. Всё остановил." if "активной игровой задачи нет" in reply else reply)
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
            turn = GameTurn(sender, "voice" if voice_ticket is not None else "chat", self.epoch)
            try:
                self.messages.put_nowait((turn, content, revision, voice_ticket))
                self.app.runtime.interrupt()
                self.event("chat.queued", {"actor": sender, "source": turn.source, "text": content[:500]})
            except queue.Full:
                self.event("chat.dropped.queue_full", {"actor": sender})

    def control_reply(self, reply):
        with self.app.guard, self.lock:
            if self.entry is not None and not self.shutdown.is_set():
                try:
                    self.entry.ChatMessage(chat_text(reply))
                except Exception:
                    pass  # A failed chat acknowledgement must not undo the stop itself.

    def followup_task(self, turn):
        task = self.task
        if not task or task["id"] != turn.task_id or task["state"] not in ("arrived", "not_arrived"):
            return None
        if (not self.entry.inGame() or navigation.active(self.entry)
                or not navigation.stationary(self.entry, self.settings.minecraft_bot)):
            return None
        player = visible_player(self.entry, turn.sender, navigation.APPROACH_RANGE)
        if player and (task["state"] != "arrived" or player[1] <= navigation.APPROACH_RADIUS):
            return task
        return None

    def speaker_observation(self, turn):
        result = {"nickname": turn.sender, "source": turn.source, "visible": None}
        if self.entry is None or turn.epoch != self.epoch:
            return result
        try:
            player = visible_player(self.entry, turn.sender, navigation.APPROACH_RANGE)
        except Exception:
            return result
        result["visible"] = player is not None
        if player is not None:
            result["distance"] = round(player[1], 2)
        return result

    def work_once(self):
        with self.app.guard:
            try:
                turn, text, revision, voice_ticket = self.messages.get_nowait()
            except queue.Empty:
                return False
            self.handling_message = True
        started = time.monotonic()
        epoch = turn.epoch
        owner = self.app.workplace.is_owner(turn.actor)
        token = text.partition(" ")[0].casefold()
        try:
            if token.startswith("/") and (not owner or token not in CONTROL):
                return True
            with self.app.guard:
                if (revision != self.app.store.get("response_revision", 0)
                        or self.entry is None or epoch != self.epoch
                        or voice_ticket is not None and not self.app.voice.current(voice_ticket)):
                    return True
                result = None
                if turn.task_id is not None:
                    task = self.followup_task(turn)
                    if task is None:
                        return True
                    text = json.dumps({"approach_outcome": task["state"], "observation": task["detail"],
                                       "instruction": "Continue the conversation with the observed speaker in your "
                                                      "current persona. Write one brief reply based on the outcome."},
                                      ensure_ascii=False)
                elif token in CONTROL:
                    result = self.app.command(text, actor=turn.actor, expected_revision=revision)
                    voice_ticket = None  # Operator controls may switch or stop the voice session.
                    revision = self.app.store.get("response_revision", 0)
            if result is None:
                answer = self.app.reply(text, actor=turn.actor,
                                        expected_revision=revision, owner=owner,
                                        cancel_event=voice_ticket.cancel if voice_ticket is not None else None,
                                        game_turn=turn)
                result, revision = answer["reply"], answer["_revision"]
                if answer.get("_transition"):
                    voice_ticket = None
            parts = chat_chunks(result)
            reply = " ".join(parts)
            if turn.source == "voice":
                self.app.voice.metrics["reply_ms"] = round((time.monotonic() - started) * 1000)
            for index, part in enumerate(parts):
                with self.app.guard, self.lock:
                    if (self.entry is None or epoch != self.epoch or self.shutdown.is_set()
                            or revision != self.app.store.get("response_revision", 0)
                            or voice_ticket is not None and not self.app.voice.current(voice_ticket)
                            or turn.task_id is not None and self.followup_task(turn) is None):
                        return True
                    self.entry.ChatMessage(part)
                if index < len(parts) - 1 and self.shutdown.wait(3):
                    return True
            with self.app.guard, self.lock:
                if (reply and revision == self.app.store.get("response_revision", 0) and epoch == self.epoch
                        and (voice_ticket is None or self.app.voice.current(voice_ticket))):
                    self.event("reply.submitted", {"actor": turn.sender, "chars": len(reply), "source": turn.source})
                    self.app.voice.enqueue(reply, revision, epoch, ticket=voice_ticket)
            return True
        except (Cancelled, ModelError, ValueError):
            return True
        finally:
            with self.app.guard:
                self.handling_message = False

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
        self.epoch = service.epoch

    def isStarted(self):
        return not self.service.shutdown.is_set()

    def onVerifedChat(self, message=None):
        if message is not None:
            self.service.accept({str(k): str(v) for k, v in message.items()}, expected_epoch=self.epoch)
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
        if isinstance(player, str) and self.service.entry is not None and self.epoch == self.service.epoch:
            self.service.app.voice.feed(player, bytes(audio), game_epoch=self.epoch)
    def agentCommandRequest(self, command): return "not supported"

    class Java:
        implements = ["adris.altoclef.PythonCallback"]
