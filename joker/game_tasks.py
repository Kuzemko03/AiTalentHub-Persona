"""Observed resource collection and bounded handover. No arbitrary game commands."""
import math
import time

TERMINAL = {"collected", "delivered_unverified", "idle_unverified", "failed", "arrived"}


def inventory(entry):
    data = entry.getInventoryFull()
    if not data or not data.get("ok"):
        raise ValueError("Не удалось прочитать инвентарь.")
    slots = []
    for raw in data.get("slots", []):
        slot = {str(k): v for k, v in raw.items()}
        if slot.get("empty"):
            continue
        index, count = int(slot["index"]), int(slot["count"])
        if 0 <= index < 36 and count > 0:
            slots.append({"slot": index, "item": str(slot["item"]).removeprefix("minecraft:"), "count": count})
    return slots


def total(slots, item):
    return sum(s["count"] for s in slots if s["item"] == item)


def owner_distance(entry, owner):
    for player in entry.getPlayersInfo(64):
        if str(player.get("name", "")).casefold() == owner.casefold():
            distance = float(player.get("distance", "nan"))
            return distance if math.isfinite(distance) else float("inf")
    return float("inf")


def advance(service, busy):
    """Called under app.guard, so Stop cannot race a new movement/drop action."""
    task, entry = service.task, service.entry
    if not task or task["kind"] not in ("collect", "deliver") or task["state"] in TERMINAL:
        return
    now = time.monotonic()
    if now - task["started_at"] > 900:
        entry.stopPathing()
        task.update(state="failed", detail="Истекли 15 минут. Проверь игру.")
        return
    slots = inventory(entry)
    have = total(slots, task["item"])
    task["inventory_count"] = have
    phase = task.get("phase", "collect")
    if phase == "collect":
        if have >= task["count"]:
            if task["kind"] == "collect":
                task.update(state="collected", detail="Нужное количество подтверждено в инвентаре Джокера.")
                # Stop a lingering gather task before accepting a different action.
                entry.stopPathing()
            else:
                entry.stopPathing()
                task.update(phase="wait_collect_stop", state="running")
        elif busy:
            task["state"] = "running"
        elif task["state"] == "running":
            task.update(state="idle_unverified", detail="Добыча прекратилась; нужного количества в инвентаре нет.")
        return
    if phase == "wait_collect_stop":
        if not busy:
            entry.ChatMessage(";followPlayer " + task["target"])
            task.update(phase="approach", state="running", detail="Иду к владельцу с ресурсом.")
        return
    if phase == "approach":
        if owner_distance(entry, task["target"]) <= 3:
            entry.stopPathing()
            task.update(phase="wait_follow_stop", detail="Останавливаюсь перед передачей.")
        return
    if phase == "wait_follow_stop":
        if not busy:
            task.update(phase="handover", handed_over=0)
        return
    if phase != "handover":
        return
    pending = task.get("pending_drop")
    if pending:
        delta = pending["before"] - have
        if delta == pending["count"]:
            task["handed_over"] += delta
            del task["pending_drop"]
        elif delta != 0 or now - pending["at"] > 3:
            task.update(state="unconfirmed", phase="manual_check",
                        detail="Неоднозначное изменение инвентаря. Автоповтора передачи нет.")
            return
        else:
            return
    remaining = task["count"] - task["handed_over"]
    if remaining == 0:
        task.update(state="delivered_unverified", detail="Предметы выброшены рядом с владельцем; его подбор нужно проверить.")
        return
    if busy or owner_distance(entry, task["target"]) > 3:
        task["detail"] = "Жду владельца рядом для передачи."
        return
    # Never click a chest/menu slot using player-inventory indices.
    if entry.getOpenScreen().get("open"):
        task["detail"] = "Закрой меню/сундук в клиенте Джокера для передачи."
        return
    if have < remaining:
        task.update(state="unconfirmed", phase="manual_check", detail="Ресурс исчез до передачи. Проверь инвентарь.")
        return
    slot = next(s for s in slots if s["item"] == task["item"])
    amount = slot["count"] if slot["count"] <= remaining else 1
    window_slot = 36 + slot["slot"] if slot["slot"] < 9 else slot["slot"]
    if not entry.lookAtPlayer(task["target"]):
        return
    # Record BEFORE issuing the click. A lost ack must never cause a duplicate throw.
    task["pending_drop"] = {"before": have, "count": amount, "at": now}
    if not entry.clickUiSlot(window_slot, 1 if amount == slot["count"] else 0, "THROW"):
        task.update(state="unconfirmed", phase="manual_check", detail="Передача не подтверждена; повтор отключён.")
