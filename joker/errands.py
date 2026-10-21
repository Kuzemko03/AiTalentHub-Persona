"""Durable, owner-controlled errands. Models propose steps; observations prove them."""
import copy
import uuid

from .tool_specs import ERRAND_CREATE as CREATE, ERRAND_CONTROL as CONTROL, obj, string, validate


DECISION = obj({"action": {"type": "string", "enum": ["start", "wait", "pause"]},
                "step_id": string(40), "seconds": {"type": "integer", "minimum": 60, "maximum": 3600},
                "note": string(500)})


class QueueConflict(ValueError):
    pass


class Errands:
    def __init__(self, app):
        self.app = app
        self.state = app.store.get("errands", None)
        if self.owns_game():
            self.state["status"] = "paused"
            step = self.current()
            if step and step["status"] in ("dispatching", "running"):
                step.update(status="review", result="Перезапуск: проверь игру. Отправленное действие не повторяется.")
            self.save("Восстановлено на паузе; запуск только владельцем.")

    def save(self, detail):
        self.state["detail"] = detail
        self.state["version"] += 1
        self.app.store.set("errands", self.state)
        # Plans may contain private owner text. Never put them in shared game experience.
        self.app.experience.record("owner", "errands", "queue.changed", {
            "id": self.state["id"], "version": self.state["version"], "status": self.state["status"],
            "detail": detail})

    def status(self):
        with self.app.guard:
            return copy.deepcopy(self.state)

    def context(self, actor):
        state = self.status()
        if state and actor != "owner":
            state.pop("title", None)
            state.pop("detail", None)
            for step in state["steps"]:
                for key in ("note", "result"):
                    step.pop(key, None)
        return state

    def owns_game(self):
        return bool(self.state and self.state["status"] not in ("finished", "cancelled"))

    def current(self):
        return next((s for s in (self.state or {}).get("steps", []) if s["status"] != "done"), None)

    def ready(self):
        step = self.current()
        return bool(self.owns_game() and self.state["status"] == "active" and step and step["status"] == "pending")

    def check_version(self, queue_id, version):
        if (not self.state or queue_id != self.state["id"] or type(version) is not int
                or version != self.state["version"]):
            raise QueueConflict("Список поручений изменился. Обнови его и повтори управление.")

    def create(self, title, steps):
        validate({"title": title, "steps": steps}, CREATE)
        if not title.strip():
            raise ValueError("Нужно название списка поручений.")
        for step in steps:
            if step["action"] == "come":
                if step["item"] != "" or step["count"] != 1:
                    raise ValueError("Для подхода нужны item='' и count=1.")
            elif step["item"] not in self.app.minecraft.resources:
                raise ValueError("Ресурс отсутствует в каталоге.")
        with self.app.guard:
            if self.owns_game():
                raise ValueError("Сначала заверши или отмени прежний список поручений.")
            game = self.app.minecraft.status()
            if game["busy"] or game["task"] and game["task"]["state"] not in ("collected", "arrived"):
                raise ValueError("Сначала проверь и останови прежнюю игровую задачу.")
            self.app.runtime.pause()
            self.state = {"id": uuid.uuid4().hex, "version": 0, "title": title.strip(), "status": "paused",
                "steps": [{**s, "id": uuid.uuid4().hex, "status": "pending", "task_id": "", "result": ""} for s in steps]}
            self.save("Список сохранён на паузе. Проверь шаги и запусти его.")
            return self.status()

    def pause(self):
        """Called BEFORE Stop clears the game task. Preserve already observed success."""
        if not self.owns_game():
            return
        step = self.current()
        changed = self.state["status"] != "paused"
        if step and step["status"] in ("dispatching", "running"):
            step.update(status="review", result="Действие прервано. Проверь результат; автоповтора нет.")
            changed = True
        self.state["status"] = "paused"
        detail = "Поручения на паузе. Шаги сохранены."
        if changed or self.state["detail"] != detail:
            self.save(detail)

    def control(self, queue_id, version, action):
        validate({"id": queue_id, "version": version, "action": action}, CONTROL)
        with self.app.guard:
            self.check_version(queue_id, version)
            if not self.owns_game():
                raise ValueError("Этот список уже закрыт.")
            if action in ("pause", "cancel"):
                reply = self.app.stop()
                if action == "cancel":
                    self.state["status"] = "cancelled"
                    self.save("Список отменён. Результат остановки игры: " + reply)
                else:
                    self.save("Поручения на паузе. Результат остановки игры: " + reply)
                return self.status()
            if self.app.runtime.sleeping():
                raise ValueError("Сначала разбуди Джокера.")
            if self.current()["status"] != "pending":
                raise ValueError("Сначала проверь прерванный или неподтверждённый шаг в панели.")
            game = self.app.minecraft.status()
            if not game["connected"] or game["busy"] or (game["task"]
                    and game["task"]["state"] not in ("collected", "arrived")):
                raise ValueError("Нужен подключённый мир без непроверенной игровой задачи.")
            self.app.runtime.pause()
            self.state["status"] = "active"
            self.save("Поручения включены. Следующее действие выберет модель.")
            self.app.runtime.next_tick = 0
            return self.status()

    def observe(self, task, busy):
        """Only a matching task's observed outcome can complete a step automatically."""
        if not self.owns_game():
            return
        step = self.current()
        if not step or step["status"] not in ("dispatching", "running"):
            return
        if not task or task.get("id") != step["task_id"]:
            return self.review("Связь шага с игровой задачей потеряна. Нужна проверка владельца.")
        observed = task.get("state")
        expected = {"collect": "collected", "come": "arrived"}.get(step["action"])
        if expected and observed == expected and not busy:
            self.done("Наблюдение игры: " + task.get("detail", observed))
        elif observed in ("unconfirmed", "failed", "idle_unverified", "delivered_unverified"):
            self.review(task.get("detail", "Результат требует проверки владельца."))

    def review(self, detail):
        self.current().update(status="review", result=detail)
        self.state["status"] = "paused"
        self.save(detail)

    def done(self, result):
        self.current().update(status="done", result=result)
        if self.current() is None:
            self.state["status"] = "finished"
        self.save("Список завершён." if self.current() is None else "Шаг завершён; ожидаю следующего решения.")

    def confirm(self, queue_id, version, step_id):
        """Explicit operator attestation, intentionally unavailable as a model tool."""
        with self.app.guard:
            self.check_version(queue_id, version)
            step = self.current()
            if not self.owns_game() or not step or step["id"] != step_id or step["status"] != "review":
                raise ValueError("Этот шаг сейчас не ожидает ручной проверки.")
            game = self.app.minecraft.status()
            if not game["connected"] or game["busy"] or game["task"] is not None:
                raise ValueError("Проверь игру и нажми Стоп. Нужен подключённый мир без активной задачи.")
            self.done("Владелец вручную подтвердил результат в панели. Это не автоматическое наблюдение.")
            return self.status()

    def apply(self, decision, snapshot, *, cancel, revision, epoch):
        validate(decision, DECISION)
        self.check_version(snapshot["id"], snapshot["version"])
        step = self.current()
        if not self.ready() or decision["step_id"] != step["id"]:
            raise ValueError("Модель выбрала недоступный или устаревший шаг.")
        if decision["action"] == "pause":
            self.pause()
        elif decision["action"] == "wait":
            self.save("Решение модели: " + decision["note"])
        else:
            # Durable intent and stable effect key BEFORE the bridge call, including crash windows.
            step.update(status="dispatching", task_id=self.state["id"] + ":" + step["id"])
            self.save("Начинаю текущий шаг: " + decision["note"])
            try:
                result = self.app.workplace.execute("game." + step["action"],
                    {"item": step["item"], "count": step["count"]} if step["action"] != "come" else {},
                    actor="owner", owner=True, cancel=cancel, revision=revision, epoch=epoch,
                    turn_id="errand:" + step["task_id"], game_task_id=step["task_id"])
                if result.get("accepted") is True:
                    step.update(status="running", result=result["detail"])
                    self.save("Шаг отправлен; жду наблюдаемого результата.")
                else:
                    self.review(result.get("detail", "Приём шага не подтверждён."))
            except Exception:
                self.review("Шаг не подтверждён. Проверь игру; автоматического повтора нет.")
                raise

    def command(self, text, actor="owner"):
        token, _, action = text.strip().partition(" ")
        if token.casefold() not in ("/поручения", "/queue"):
            return None
        with self.app.guard:
            if not action:
                import json
                if actor != "owner" and self.state:
                    return "Поручения: " + self.state["status"] + "; выполнено " + str(
                        sum(s["status"] == "done" for s in self.state["steps"])) + "/" + str(len(self.state["steps"]))
                return json.dumps(self.status(), ensure_ascii=False, indent=2) if self.state else "Список поручений пока не создан."
            mapping = {"пуск": "resume", "resume": "resume", "пауза": "pause", "pause": "pause",
                       "отмена": "cancel", "cancel": "cancel"}
            if action.casefold() not in mapping or not self.state:
                return "/поручения [пуск | пауза | отмена]. Создать список можно обычной просьбой."
            return self.control(self.state["id"], self.state["version"], mapping[action.casefold()])["detail"]
