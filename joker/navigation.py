"""Short, model-chosen exploration legs, checked against actual client navigation."""
import math
import time
import uuid


def surface(entry, me):
    if not me or "position" not in me:
        return []
    x, y, z = (math.floor(v) for v in me["position"])
    rows = []
    for raw in entry.getSurfaceMap(3):
        try:
            dx, dz, dy = (int(str(raw.get(k))) for k in ("dx", "dz", "top_dy"))
        except (TypeError, ValueError):
            continue
        if -3 <= dx <= 3 and -3 <= dz <= 3 and -8 <= dy <= 8:
            rows.append({"x": x + dx, "y": y + dy + 1, "z": z + dz,
                         "ground": str(raw.get("block", ""))[:80]})
    return rows[:49]


def validate_point(x, y, z):
    if (any(type(v) is not int for v in (x, y, z)) or not -30000000 <= x <= 30000000
            or not -30000000 <= z <= 30000000 or not -64 <= y <= 319):
        raise ValueError("Нужны целочисленные координаты в границах мира.")


def start(service, x, y, z, task_id=None):
    validate_point(x, y, z)
    me = service.observe().get("self")
    if not me or "position" not in me:
        return {"accepted": False, "completed": False, "detail": "Не удалось прочитать своё положение. Движение не начато."}
    px, py, pz = me["position"]
    if math.hypot(x + .5 - px, z + .5 - pz) > 32 or abs(y - py) > 8:
        return {"accepted": False, "completed": False, "detail": "Выбери ближний участок: до 32 блоков по горизонтали и 8 по высоте."}
    task = {"id": task_id or uuid.uuid4().hex, "kind": "walk", "target": {"x": x, "y": y, "z": z},
            "state": "submitted", "started_at": time.monotonic(), "last_progress_at": time.monotonic(),
            "last_position": me["position"]}
    service.task = task
    service.app.store.set("pending_game_action", {k: v for k, v in task.items() if k != "started_at"})
    result = service.entry.gotoXYZ(x, y, z)
    if not result or not result.get("ok"):
        task.update(state="unconfirmed", detail="Навигация не подтвердила приём точки. Проверь игру; повтор не выполняется.")
        service.trace_task()
        return {"accepted": None, "completed": False, "detail": task["detail"], "task": dict(task)}
    service.trace_task()
    return {"accepted": True, "completed": False, "task": dict(task),
            "detail": "Ближняя точка передана навигатору. Прибытие ещё проверяется."}


def refresh(service, busy):
    task = service.task
    if task["state"] in ("arrived", "failed", "unconfirmed", "not_arrived"):
        return
    elapsed = time.monotonic() - task["started_at"]
    if task.get("phase") == "arrival_stopping":
        if not busy:
            task.update(state="arrived", detail="Навигатор подтвердил прибытие; остановка запрошена, активной задачи нет.")
        return
    if task.get("phase") in ("timeout_stopping", "blocked_stopping"):
        if not busy:
            task.update(state="not_arrived", detail="Участок пути не завершён; остановка подтверждена. Нужен новый маршрут.")
        return
    if elapsed > 90:
        service.entry.stopPathing()
        task.update(state="running", phase="timeout_stopping", detail="Истекло время участка; проверяется остановка.")
        return
    status = service.entry.pathStatus()
    if not status or not status.get("ok"):
        return  # A client-thread timeout proves neither arrival nor failure.
    expected = ",".join(str(task["target"][k]) for k in ("x", "y", "z"))
    if str(status.get("goal", "")) != expected:
        if elapsed > 3:
            task.update(state="unconfirmed", detail="Цель навигатора изменилась или не подтверждена. Автоматический повтор отключён.")
        return
    if status.get("arrived") is True:
        service.entry.stopPathing()
        task.update(state="running", phase="arrival_stopping")
        return
    me = service.observe().get("self")
    position = me.get("position") if me else None
    if position is not None and position != task.get("last_position"):
        task.update(last_position=position, last_progress_at=time.monotonic())
    if not busy and position is not None and time.monotonic() - task["last_progress_at"] > 8:
        service.entry.stopPathing()
        task.update(state="running", phase="blocked_stopping", detail="Нет прогресса по положению; проверяется остановка перед новым маршрутом.")
    else:
        task["state"] = "running"  # Native busy alone does not cover every navigator phase.
