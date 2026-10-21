"""Bounded local experience and durable tool receipts, isolated by visibility."""
import json

from .tool_specs import STATE, validate


class Experience:
    LIMIT = 1000

    def __init__(self, app):
        self.app, self.store = app, app.store

    @staticmethod
    def scope(actor):
        return "world:minecraft" if actor.startswith("minecraft:") else actor

    def record(self, actor, source, kind, data, turn_id=""):
        # Callback data is untrusted and must stay bounded; never stringify Java objects here.
        payload = json.dumps(data, ensure_ascii=False, allow_nan=False)
        if len(payload) > 3000:
            payload = json.dumps({"summary": payload[:2400], "truncated": True}, ensure_ascii=False)
        scope = self.scope(actor)
        with self.store.lock, self.store.db:
            self.store.db.execute("INSERT INTO experience_events(scope,source,kind,actor,turn_id,at,payload) "
                "VALUES (?,?,?,?,?,?,?)", (scope, source[:40], kind[:60], actor[:120], turn_id[:120], self.store.now(), payload))
            self.store.db.execute("DELETE FROM experience_events WHERE scope=? AND id NOT IN "
                "(SELECT id FROM experience_events WHERE scope=? ORDER BY id DESC LIMIT ?)", (scope, scope, self.LIMIT))

    def recent(self, actor, limit=12):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Неверный размер журнала.")
        with self.store.lock:
            rows = self.store.db.execute("SELECT id,source,kind,actor,turn_id,at,payload FROM experience_events "
                "WHERE scope=? ORDER BY id DESC LIMIT ?", (self.scope(actor), limit)).fetchall()
        return [{**{k: row[k] for k in row.keys() if k != "payload"}, "data": json.loads(row["payload"])}
                for row in reversed(rows)]

    def state(self, actor):
        return self.store.get("character_state:" + self.scope(actor), {})

    def update(self, actor, value):
        validate(value, STATE)
        value = {k: v.strip() for k, v in value.items()}
        if value != self.state(actor):
            self.store.set("character_state:" + self.scope(actor), value)
            self.record(actor, "character", "state.changed", value)

    def context(self, actor):
        events, used = [], 0
        for event in reversed(self.recent(actor)):
            size = len(json.dumps(event, ensure_ascii=False))
            if used + size > 7000:
                break
            events.append(event)
            used += size
        return {"state": self.state(actor), "events": list(reversed(events))}

    def claim_effect(self, turn_id, actor, name):
        # Pending means the process may have crashed after dispatch. Never replay it.
        with self.store.lock, self.store.db:
            inserted = self.store.db.execute("INSERT OR IGNORE INTO tool_effects VALUES (?,?,?,'pending','{}',?)",
                (turn_id, actor, name, self.store.now())).rowcount
            row = self.store.db.execute("SELECT name,status,result FROM tool_effects WHERE turn_id=? AND actor=?",
                                       (turn_id, actor)).fetchone()
            if row is None:
                raise ValueError("Идентификатор действия принадлежит другой беседе.")
        return bool(inserted), {"name": row["name"], "status": row["status"], "result": json.loads(row["result"])}

    def finish_effect(self, turn_id, value):
        with self.store.lock, self.store.db:
            self.store.db.execute("UPDATE tool_effects SET status='returned',result=? WHERE turn_id=?",
                (json.dumps(value, ensure_ascii=False), turn_id))
