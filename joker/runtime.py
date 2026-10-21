"""Persistent, opt-in agent loop. Decisions use a small, validated tool set."""
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from .tool_specs import STATE, validate
from .backends import Cancelled, ModelError
from .errands import DECISION as ERRAND_DECISION

PLAN_SCHEMA = {"type": "object", "properties": {
    "state": {"anyOf": [{"type": "null"}, STATE]},
    "action": {"type": "string", "enum": ["wait", "follow", "come", "collect", "deliver", "sleep"]},
    "item": {"type": "string", "maxLength": 80},
    "count": {"type": "integer", "minimum": 1, "maximum": 256},
    "seconds": {"type": "integer", "minimum": 60, "maximum": 3600},
    "note": {"type": "string", "maxLength": 500}},
    "required": ["action", "item", "count", "seconds", "note", "state"], "additionalProperties": False}


class Runtime:
    def __init__(self, app):
        self.app = app
        self.shutdown = threading.Event()
        self.thread = None
        self.next_tick = 0
        self.planning = False
        self.state = {"enabled": False, "mode": "awake", "goal": "", "wake_at": 0,
                      "resume_enabled": False, "detail": "Самостоятельная работа выключена."}
        self.state.update(app.store.get("runtime", {}))
        if self.state.get("pending_action"):
            self.state.update(enabled=False, resume_enabled=False,
                detail="План восстановлен. Проверь незавершённую игровую задачу перед включением автономности.")

    def save(self):
        self.app.store.set("runtime", self.state)

    def status(self):
        with self.app.guard:
            return {**self.state, "plan": self.app.store.get("runtime_plan", []),
                    "daily_limit": self.app.settings.runtime_daily_requests}

    def pause(self):
        with self.app.guard:
            self.state.update(enabled=False, resume_enabled=False,
                              detail="Самостоятельная работа на паузе. Цель и план сохранены.")
            self.save()

    def sleeping(self):
        return self.state["mode"] == "sleep"

    def interrupt(self):
        with self.app.guard:
            if self.planning and self.app.active_cancel:
                self.app.active_cancel.set()

    def sleep(self, seconds=0, scheduled=False):
        with self.app.guard:
            resume = self.state["enabled"] if scheduled else False
            self.app.stop()
            self.state.update(mode="sleep", wake_at=time.time() + seconds if seconds else 0,
                              resume_enabled=resume, detail="Сплю. План сохранён.")
            self.save()
        return "Сплю. План сохранён. Разбудить: /проснись."

    def wake(self):
        with self.app.guard:
            self.state.update(mode="awake", wake_at=0, enabled=self.state["resume_enabled"],
                              resume_enabled=False, detail="Бодрствую.")
            self.save()
            self.next_tick = 0
        return "Проснулся. Помню цель и последний план."

    def command(self, text):
        token, _, argument = text.strip().partition(" ")
        token = token.casefold()
        if token in ("/сон", "/sleep"):
            if argument and (not argument.isdecimal() or not 1 <= int(argument) <= 1440):
                return "Формат: /сон [минуты от 1 до 1440]."
            return self.sleep(int(argument) * 60 if argument else 0)
        if token in ("/проснись", "/wake"):
            return self.wake()
        if token in ("/цель", "/goal"):
            with self.app.guard:
                if argument:
                    if len(argument) > 1000:
                        return "Опиши цель в пределах 1000 символов."
                    self.app.stop()
                    self.state["goal"] = argument
                    self.state.pop("pending_action", None)
                    self.app.store.set("runtime_plan", [])
                    self.save()
                return "Цель: " + (self.state["goal"] or "не задана")
        if token in ("/автономность", "/autonomy"):
            with self.app.guard:
                if argument.casefold() in ("вкл", "on"):
                    if not self.state["goal"]:
                        return "Сначала задай /цель, затем /автономность вкл."
                    if self.sleeping():
                        return "Сначала разбуди: /проснись."
                    self.state.update(enabled=True, detail="Самостоятельная работа включена.")
                    self.state.pop("pending_action", None)
                    self.next_tick = 0
                    self.save()
                elif argument.casefold() in ("выкл", "off"):
                    self.app.stop()
                    self.state["detail"] = "Самостоятельная работа выключена."
                    self.save()
                return self.state["detail"]
        if token in ("/план", "/plan"):
            return json.dumps(self.status(), ensure_ascii=False, indent=2)
        return None

    def quiet_remaining(self, now):
        value = self.app.settings.quiet_hours
        if not value:
            return 0
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("QUIET_HOURS: ожидается 23:00-08:00.")
        start, end = (int(p[:2]) * 60 + int(p[3:]) for p in value.split("-"))
        if start == end:
            raise ValueError("Начало и конец QUIET_HOURS должны различаться.")
        local = datetime.fromtimestamp(now, timezone(timedelta(hours=self.app.settings.utc_offset)))
        minute = local.hour * 60 + local.minute
        inside = start <= minute < end if start < end else minute >= start or minute < end
        return ((end - minute) % 1440) * 60 - local.second if inside else 0

    def tick(self, now=None):
        now = time.time() if now is None else now
        with self.app.guard:
            quiet = self.quiet_remaining(now)
            if self.sleeping():
                if self.state["wake_at"] and now >= self.state["wake_at"] and not quiet:
                    self.wake()
                else:
                    return
            if quiet:
                self.sleep(quiet, scheduled=True)
                return
            queue_mode = self.app.errands.ready()
            if self.app.errands.owns_game():
                if not queue_mode:
                    return
            elif not self.state["enabled"] or not self.state["goal"]:
                return
            if now < self.next_tick:
                return
            self.next_tick = now + max(15, self.app.settings.runtime_interval)
            game = self.app.minecraft.status()
            if not game["connected"]:
                self.state["detail"] = "Жду подключения Minecraft. Модель не вызывается."
                return
            if game["task"] and game["task"]["state"] in ("unconfirmed", "idle_unverified", "delivered_unverified", "failed"):
                self.pause()
                self.state["detail"] = "Проверь результат игровой задачи. Автономность на паузе."
                self.save()
                return
            if game["busy"] or game["task"] and game["task"]["state"] not in ("collected", "arrived"):
                return
            day = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
            budget = self.app.store.get("runtime_budget", {"day": day, "used": 0})
            if budget["day"] != day:
                budget = {"day": day, "used": 0}
            if budget["used"] >= self.app.settings.runtime_daily_requests:
                self.state["detail"] = "Дневной бюджет самостоятельных решений исчерпан."
                if queue_mode and self.app.errands.state["detail"] != self.state["detail"]:
                    self.app.errands.save(self.state["detail"])
                return
            revision = self.app.store.get("response_revision", 0)
            epoch = self.app.minecraft.epoch
            goal = self.state["goal"]
            queue = self.app.errands.status() if queue_mode else None
        # Yield the single model lane to incoming chat. Never wait behind a dialogue.
        if not self.app.generation.acquire(blocking=False):
            return
        cancel = threading.Event()
        call_id = None
        try:
            with self.app.guard:
                if (revision != self.app.store.get("response_revision", 0)
                        or not (self.app.errands.ready() if queue_mode else self.state["enabled"])):
                    return
                if queue_mode:
                    self.app.errands.check_version(queue["id"], queue["version"])
                call_id = self.app.store.reserve_call(self.app.settings.daily_requests)
                budget["used"] += 1
                self.app.store.set("runtime_budget", budget)
                self.app.active_cancel = cancel
                self.planning = True
                mask = dict(self.app.masks[self.app.active])
                plan = self.app.store.get("runtime_plan", [])
            if queue_mode:
                self.decide_errand(queue, mask, cancel, revision, epoch, now)
                self.app.store.finish_call(call_id, {"status": "done", "purpose": "errands"})
                call_id = None
                return
            system = "\n\n".join((self.app.home / file).read_text(encoding="utf-8")
                                   for file in ("PERSONA.md", "PLANNING.md"))
            result = self.app.backend.generate(system, [{"role": "user", "content": json.dumps(
                {"goal": goal, "game": game, "recent_plan": plan[-8:], "mask": mask,
                    "resources": self.app.minecraft.resources, "experience": self.app.experience.context(
                    "minecraft:" + self.app.settings.minecraft_owner.casefold()),
                    "game_memories": self.app.memory.recall(
                    "minecraft:" + self.app.settings.minecraft_owner.casefold(), goal)}, ensure_ascii=False)}],
                cancel, schema=PLAN_SCHEMA)
            action = result.get("action")
            count, seconds = result.get("count"), result.get("seconds")
            if (action not in ("wait", "follow", "come", "collect", "deliver", "sleep")
                    or not isinstance(result.get("item"), str) or result["item"] not in self.app.minecraft.resources
                    or type(count) is not int or not 1 <= count <= 256
                    or type(seconds) is not int or not 60 <= seconds <= 3600
                    or not isinstance(result.get("note"), str) or len(result["note"]) > 500):
                raise ValueError("Некорректный план модели.")
            if result.get("state") is not None:
                validate(result["state"], STATE)
            with self.app.guard:
                if (self.shutdown.is_set() or cancel.is_set() or revision != self.app.store.get("response_revision", 0)
                        or not self.state["enabled"] or epoch != self.app.minecraft.epoch):
                    raise Cancelled("Решение отменено.")
                if action == "sleep":
                    outcome = self.sleep(seconds, scheduled=True)
                elif action == "wait":
                    self.next_tick = now + seconds
                    outcome = "Ожидание."
                else:
                    self.state["pending_action"] = {"action": action, "item": result["item"], "count": count}
                    self.save()
                    actor = "minecraft:" + self.app.settings.minecraft_owner.casefold()
                    observation = self.app.workplace.execute("game." + action,
                        {"item": result["item"], "count": count} if action in ("collect", "deliver") else {},
                        actor=actor, owner=True, cancel=cancel, revision=revision, epoch=epoch,
                        turn_id="autonomy:" + uuid.uuid4().hex)
                    outcome = observation["detail"]
                if result.get("state") is not None and action != "sleep":
                    self.app.experience.update("minecraft:" + self.app.settings.minecraft_owner.casefold(), result["state"])
                record = {"at": self.app.store.now(), "action": action, "item": result["item"],
                          "count": count, "note": result["note"], "result": outcome}
                self.app.store.set("runtime_plan", (plan + [record])[-20:])
                self.state["detail"] = outcome
                self.save()
            self.app.store.finish_call(call_id, {"status": "done", "purpose": "runtime"})
            call_id = None
        except Cancelled:
            pass
        except (ModelError, ValueError):
            with self.app.guard:
                self.state["detail"] = "Самостоятельное решение не выполнено. Повтор позже."
                if (queue_mode and self.app.errands.ready() and self.app.errands.state["id"] == queue["id"]
                        and self.app.errands.state["version"] == queue["version"]):
                    self.app.errands.save("Решение модели не выполнено. Шаг не отправлен; новое решение в пределах бюджета.")
        finally:
            if call_id is not None:
                self.app.store.finish_call(call_id, {"status": "failed", "purpose": "errands" if queue_mode else "runtime"})
            with self.app.guard:
                self.planning = False
                if self.app.active_cancel is cancel:
                    self.app.active_cancel = None
            self.app.generation.release()

    def decide_errand(self, queue, mask, cancel, revision, epoch, now):
        system = "\n\n".join((self.app.home / file).read_text(encoding="utf-8")
                               for file in ("PERSONA.md", "ERRANDS.md"))
        result = self.app.backend.generate(system, [{"role": "user", "content": json.dumps({
            "queue": queue, "mask": mask, "game": self.app.minecraft.observe(),
            "experience": self.app.experience.context("world:minecraft")}, ensure_ascii=False)}],
            cancel, schema=ERRAND_DECISION)
        if not isinstance(result, dict):
            raise ValueError("Модель вернула неверное решение по поручениям.")
        if set(result) - set(ERRAND_DECISION["properties"]) - {"usage", "latency", "backend", "model"}:
            raise ValueError("В решении по поручениям есть неизвестные поля.")
        result = {key: result.get(key) for key in ERRAND_DECISION["properties"]}
        validate(result, ERRAND_DECISION)
        with self.app.guard:
            self.app.workplace.check(cancel, revision, epoch)
            if self.shutdown.is_set() or self.sleeping():
                raise Cancelled("Решение по поручению отменено.")
            self.app.errands.apply(result, queue, cancel=cancel, revision=revision, epoch=epoch)
            if result["action"] == "wait":
                self.next_tick = now + result["seconds"]

    def start(self):
        self.quiet_remaining(time.time())  # Validate before creating a worker.
        if not -12 <= self.app.settings.utc_offset <= 14:
            raise ValueError("UTC_OFFSET должен быть от -12 до 14.")
        if self.thread and self.thread.is_alive():
            return
        self.shutdown.clear()
        self.thread = threading.Thread(target=self.run, daemon=True, name="joker-runtime")
        self.thread.start()

    def run(self):
        while not self.shutdown.is_set():
            try:
                day = self.app.store.now()[:10]
                if self.app.store.get("last_backup_day", "") != day:
                    from .lifecycle import snapshot
                    snapshot(self.app.store, self.app.settings.root)
                    self.app.store.set("last_backup_day", day)
                self.tick()
            except Exception:
                with self.app.guard:
                    self.state["detail"] = "Ошибка цикла. Самостоятельная работа остановлена."
                    self.pause()
            self.shutdown.wait(1)

    def close(self):
        self.shutdown.set()
        self.interrupt()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
