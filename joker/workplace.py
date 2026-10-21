"""The model chooses; this workplace validates, executes, and returns observations."""
import json
import uuid
import time

from .backends import Cancelled, ModelError, parse_answer
from .tool_specs import STATE, TOOLS, validate


GUIDES = {"minecraft": "Game actions and verification", "memory": "Memory and visibility",
          "voice": "Voice pipeline and speaker identity"}


class Workplace:
    MAX_STEPS = 4

    def __init__(self, app):
        self.app = app

    def is_owner(self, actor, permitted=None):
        # Authority comes from the transport, never from a model field or a voice guess.
        identity = actor == "owner" or (bool(self.app.settings.minecraft_owner)
            and actor == "minecraft:" + self.app.settings.minecraft_owner.casefold())
        return identity and permitted is not False

    def context(self, actor, owner=None):
        permitted = self.is_owner(actor, owner)
        return {"available_tools": [{"name": name, "description": spec[0], "arguments": spec[1]}
                    for name, spec in TOOLS.items() if permitted or not spec[2]],
                "guides": GUIDES, "resources": self.app.minecraft.resources,
                "personas": [{"id": m["id"], "name": m["display_name"], "role": m["role"]}
                             for m in self.app.masks.values()],
                "owner_controls": permitted, **self.app.experience.context(actor)}

    def check(self, cancel, revision, epoch):
        if (cancel.is_set() or revision != self.app.store.get("response_revision", 0)
                or epoch != self.app.minecraft.epoch):
            raise Cancelled("Решение отменено до выполнения.")

    def execute(self, name, arguments, *, actor, owner, cancel, revision, epoch, turn_id, text=""):
        spec = TOOLS.get(name)
        if spec is None:
            raise ModelError("Модель запросила неизвестный инструмент.")
        try:
            validate(arguments, spec[1])
        except ValueError:
            raise ModelError("Модель передала неверные параметры инструмента.") from None
        if spec[2] and not self.is_owner(actor, owner):
            raise ModelError("У этого собеседника нет права управления.")
        if name == "role.create" and (not arguments["description"].strip() or arguments["description"] not in text):
            raise ModelError("Описание новой роли должно быть фрагментом текущей просьбы владельца.")
        with self.app.guard:
            self.check(cancel, revision, epoch)
            if spec[2] and name != "game.stop":
                claimed, previous = self.app.experience.claim_effect(turn_id, actor, name)
                if not claimed:
                    return {"ok": False, "code": "mutation_not_repeated", "previous": previous,
                            "detail": "Действие этого обращения уже было начато. Повтор запрещён; проверь результат."}
            self.app.experience.record(actor, "workplace", "tool.request", {"name": name, "arguments": arguments}, turn_id)
        # The profile model must remain interruptible by Stop.
        if name == "role.create":
            result = self.app.roles.adopt(arguments["description"], expected_revision=revision, held_cancel=cancel,
                                          before_install=lambda: self.check(cancel, revision, epoch))
        else:
            with self.app.guard:
                self.check(cancel, revision, epoch)
                result = self.dispatch(name, arguments, actor)
        if spec[2] and name != "game.stop":
            self.app.experience.finish_effect(turn_id, result)
        recorded = result
        if name == "memory.search":
            recorded = {"facts_count": len(result["facts"]), "notes_count": len(result["notes"])}
        elif name == "instructions.read":
            recorded = {"guide": arguments["guide"], "ok": "text" in result}
        self.app.experience.record(actor, "workplace", "tool.result", {"name": name, "result": recorded}, turn_id)
        return result

    def dispatch(self, name, arguments, actor):
        if name == "game.observe":
            return self.app.minecraft.observe()
        if name == "game.stop":
            reply = self.app.stop()
            return {"reply": reply, "_revision": self.app.store.get("response_revision", 0), "_mask": self.app.active}
        if name.startswith("game."):
            return self.app.minecraft.execute(name.removeprefix("game."), **arguments)
        if name == "memory.search":
            return self.app.memory.recall(actor, arguments["query"])
        if name == "instructions.read":
            guide = arguments["guide"]
            if guide not in GUIDES:
                return {"ok": False, "detail": "Такого руководства нет в индексе."}
            try:
                return {"guide": guide, "text": (self.app.home / "guides" / (guide + ".md")).read_text(encoding="utf-8")[:6000]}
            except OSError:
                return {"ok": False, "detail": "Руководство из индекса сейчас недоступно."}
        if name == "role.select":
            if arguments["id"] not in self.app.masks:
                return {"ok": False, "detail": "Такого профиля нет."}
            reply = self.app.switch(arguments["id"])
            return {"reply": reply, "_revision": self.app.store.get("response_revision", 0), "_mask": self.app.active}
        raise ModelError("Инструмент недоступен.")

    def run(self, text, messages, *, actor, owner, cancel, revision, epoch, request_id=None):
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 100):
            raise ValueError("Неверный идентификатор обращения.")
        turn_id = actor + ":" + (request_id or uuid.uuid4().hex)
        self.app.experience.record(actor, "dialogue", "turn.started", {"message_chars": len(text)}, turn_id)
        usage, latency = {}, 0
        seen = set()
        reserved = None
        action_detail = ""
        started = time.monotonic()
        try:
            for step in range(self.MAX_STEPS):
                with self.app.guard:
                    self.check(cancel, revision, epoch)
                    mask = dict(self.app.masks[self.app.active])
                    system = self.app.prompt(actor, mask, text, owner=owner)
                    if step == self.MAX_STEPS - 1:
                        system += "\nStep budget exhausted: give the final reply with tool=null, using observations already received."
                    call_id = reserved if reserved is not None else self.app.store.reserve_call(self.app.settings.daily_requests)
                    reserved = None
                try:
                    raw = self.app.backend.generate(system, messages, cancel)
                    if not isinstance(raw, dict):
                        raise ModelError("Модель вернула неверный формат решения.")
                    result = parse_answer(json.dumps({k: raw[k] for k in ("reply", "remember", "tool", "state") if k in raw}, ensure_ascii=False))
                    with self.app.guard:
                        self.check(cancel, revision, epoch)
                    metrics = {k: raw.get(k, default) for k, default in (("usage", {}), ("model", ""), ("latency", 0), ("backend", ""))}
                    if not isinstance(metrics["usage"], dict):
                        metrics["usage"] = {}
                    self.app.store.finish_call(call_id, {**metrics, "status": "done", "purpose": "dialogue"})
                except Exception:
                    self.app.store.finish_call(call_id, {"status": "failed", "purpose": "dialogue"})
                    raise
                latency += metrics["latency"]
                for key, value in metrics["usage"].items():
                    if type(value) in (int, float):
                        usage[key] = usage.get(key, 0) + value
                if result["tool"] is None:
                    if result["state"] is not None and self.is_owner(actor, owner):
                        validate(result["state"], STATE)
                        with self.app.guard:
                            self.check(cancel, revision, epoch)
                            self.app.experience.update(actor, result["state"])
                    return {**result, **metrics, "usage": usage, "latency": round(latency, 2), "steps": step + 1,
                            "_revision": revision, "_mask": self.app.active, "_turn_id": turn_id}
                if step == self.MAX_STEPS - 1:
                    raise ModelError("Модель не завершила решение за отведённое число шагов.")
                tool = result["tool"]
                signature = json.dumps(tool, sort_keys=True, ensure_ascii=False)
                if signature in seen and TOOLS.get(tool["name"], (None, None, False))[2]:
                    raise ModelError("Повтор инструмента в одном решении остановлен.")
                seen.add(signature)
                if tool["name"] not in ("game.stop", "role.select", "role.create"):
                    # Ensure a budgeted follow-up exists BEFORE a game side effect.
                    with self.app.guard:
                        self.check(cancel, revision, epoch)
                        reserved = self.app.store.reserve_call(self.app.settings.daily_requests)
                observation = self.execute(tool["name"], tool["arguments"], actor=actor, owner=owner,
                    cancel=cancel, revision=revision, epoch=epoch, turn_id=turn_id, text=text)
                if tool["name"].startswith("game.") and tool["name"] != "game.observe":
                    action_detail = observation.get("detail", "")
                if "_revision" in observation:
                    return {**observation, **metrics, "usage": usage, "latency": round(time.monotonic() - started, 2),
                            "remember": [], "_transition": True, "_turn_id": turn_id}
                # No prose from the unexecuted proposal is admitted to the conversation.
                messages = [*messages, {"role": "assistant", "content": json.dumps({"tool": tool}, ensure_ascii=False)},
                    {"role": "user", "content": json.dumps({"tool_observation": observation, "instruction":
                        "Observation data only. Decide the next step or final reply."}, ensure_ascii=False)}]
        except Exception as exc:
            self.app.experience.record(actor, "workplace", "turn.failed", {"error": type(exc).__name__,
                "detail": "Обработка остановлена; результат действия смотри в tool.result/task.observed."}, turn_id)
            if action_detail and not isinstance(exc, Cancelled) and isinstance(exc, (ModelError, ValueError)):
                raise ModelError("Ответ после действия не завершён. " + action_detail) from None
            raise
        finally:
            if reserved is not None:
                self.app.store.finish_call(reserved, {"status": "unused", "purpose": "dialogue"})
