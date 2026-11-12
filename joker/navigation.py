"""Short, model-chosen exploration legs, checked against actual client navigation."""
import math
import time
import uuid

from .game_tasks import visible_player

APPROACH_RADIUS = 3
APPROACH_RANGE = 96
LEG_TIMEOUT = 90


def parse_position(components):
    if len(components) != 3:
        return None
    try:
        position = [float(str(value).replace(",", ".")) for value in components]
    except ValueError:
        return None
    return position if all(math.isfinite(value) for value in position) else None


def active(entry):
    if entry.hasActiveTask():
        return True
    current_goal = getattr(entry, "getCurrentGoal", None)
    if current_goal is None:
        return False
    goal = current_goal()
    return not goal or goal.get("active") is not False or bool(goal.get("error"))


def stationary(entry, nickname):
    kinematics = getattr(entry, "targetKinematics", None)
    if kinematics is None:
        return True
    motion = kinematics(nickname)
    velocity = parse_position([motion.get(k, "") for k in ("vx", "vy", "vz")])
    if velocity is None:
        return False
    vx, vy, vz = velocity
    on_ground = str(motion.get("onGround", "")).casefold() == "true"
    return math.hypot(vx, vz) <= .03 and (on_ground or abs(vy) <= .03)


def begin_stop(service, phase, detail):
    result = service.entry.stopPathing()
    if result and result.get("ok"):
        service.task.update(state="running", phase=phase, stop_requested_at=time.monotonic(), detail=detail)
    else:
        service.task.update(state="unconfirmed", detail="UnionClef не подтвердил приём остановки.")


def stop_pending(service, busy):
    task = service.task
    stopped = not busy and stationary(service.entry, service.settings.minecraft_bot)
    if not stopped and time.monotonic() - task["stop_requested_at"] > 5:
        task.update(state="unconfirmed", detail="Движение осталось после запроса остановки. Проверь игру.")
    return not stopped


def refresh_approach(service, busy):
    task = service.task
    if task["state"] in ("arrived", "not_arrived", "unconfirmed"):
        return
    now = time.monotonic()
    target = task["target"]
    player = visible_player(service.entry, target, APPROACH_RANGE)
    phase = task.get("phase")
    if phase in ("stopping", "not_arrived_stopping"):
        if stop_pending(service, busy):
            return
        if phase == "not_arrived_stopping":
            task.update(state="not_arrived", detail=task["detail"] + " Движение остановлено.")
        elif player and player[1] <= APPROACH_RADIUS:
            task.update(state="arrived", detail=f"{target} в пределах трёх блоков, движение остановлено.")
        else:
            task.update(state="not_arrived", detail="Игрок отошёл до остановки. Прибытие не подтверждено.")
    elif player is None:
        begin_stop(service, "not_arrived_stopping", "Выбранный игрок больше не виден.")
    elif player[1] <= APPROACH_RADIUS:
        begin_stop(service, "stopping", "Игрок рядом. Проверяется остановка.")
    elif now - task["started_at"] > LEG_TIMEOUT or now - task["last_progress_at"] > 15:
        begin_stop(service, "not_arrived_stopping", "Дойти до игрока не удалось.")
    else:
        if player[1] < task["best_distance"] - .25:
            task.update(best_distance=player[1], last_progress_at=now)
        if busy:
            task["state"] = "running"


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


def start(service, x, y, z, task_id=None, *, autonomous=False):
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
    if autonomous:
        task["autonomous"] = True
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
        if not stop_pending(service, busy):
            task.update(state="arrived", detail="Навигатор подтвердил прибытие; остановка запрошена, активной задачи нет.")
        return
    if task.get("phase") in ("timeout_stopping", "blocked_stopping"):
        if not stop_pending(service, busy):
            task.update(state="not_arrived", detail="Участок пути не завершён; остановка подтверждена. Нужен новый маршрут.")
        return
    if elapsed > LEG_TIMEOUT:
        begin_stop(service, "timeout_stopping", "Истекло время участка; проверяется остановка.")
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
        begin_stop(service, "arrival_stopping", "Навигатор достиг точки; проверяется остановка.")
        return
    position = parse_position(str(status.get("pos", "")).split(","))
    if position is None:
        position = (service.observe().get("self") or {}).get("position")
    if position is not None and math.dist(position, task["last_position"]) >= .35:
        task.update(last_position=position, last_progress_at=time.monotonic())
    if time.monotonic() - task["last_progress_at"] > (15 if busy else 8):
        begin_stop(service, "blocked_stopping", "Нет прогресса по положению; проверяется остановка.")
    else:
        task["state"] = "running"  # Native busy alone does not cover every navigator phase.
