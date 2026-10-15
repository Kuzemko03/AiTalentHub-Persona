"""Private SQLite state, separate from versioned character files."""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        with self.db:
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS facts (
                  actor TEXT, fact TEXT, at TEXT, PRIMARY KEY(actor,fact));
                CREATE TABLE IF NOT EXISTS messages (
                  id INTEGER PRIMARY KEY, actor TEXT, role TEXT, content TEXT, mask TEXT, at TEXT);
                CREATE TABLE IF NOT EXISTS calls (
                  id INTEGER PRIMARY KEY, day TEXT, result TEXT, at TEXT);
                CREATE TABLE IF NOT EXISTS telegram_jobs (
                  update_id INTEGER PRIMARY KEY, chat_id INTEGER, text TEXT, status TEXT,
                  reply TEXT DEFAULT '', error TEXT DEFAULT '', revision INTEGER);
                CREATE TABLE IF NOT EXISTS channel_posts (
                  id TEXT PRIMARY KEY, created_at TEXT, published_at TEXT DEFAULT '',
                  status TEXT, payload TEXT, photo TEXT DEFAULT '', target INTEGER DEFAULT 0,
                  photo_message_id INTEGER, text_message_id INTEGER, error TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS channel_events (
                  id TEXT PRIMARY KEY, text TEXT, created_at TEXT, used_by TEXT DEFAULT '');
                CREATE TABLE IF NOT EXISTS channel_slots (
                  slot TEXT PRIMARY KEY, post_id TEXT DEFAULT '', state TEXT, created_at TEXT);
            """)

    def recover_jobs(self):
        with self.lock, self.db:
            self.db.execute("UPDATE telegram_jobs SET status='pending' WHERE status='running'")

    def close(self):
        with self.lock:
            self.db.close()

    @staticmethod
    def now():
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def facts(self, actor):
        with self.lock:
            return [r[0] for r in self.db.execute(
                "SELECT fact FROM facts WHERE actor=? ORDER BY at DESC LIMIT 24", (actor,))]

    def remember(self, actor, fact):
        if not isinstance(fact, str) or not fact.strip() or len(fact) > 1000:
            raise ValueError("Факт должен содержать от 1 до 1000 символов.")
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO facts VALUES (?,?,?)", (actor, fact.strip(), self.now()))

    def append(self, actor, role, content, mask):
        with self.lock, self.db:
            self.db.execute("INSERT INTO messages(actor,role,content,mask,at) VALUES (?,?,?,?,?)",
                            (actor, role, content, mask, self.now()))

    def history(self, actor, limit=12):
        with self.lock:
            rows = self.db.execute("SELECT role,content,mask FROM messages WHERE actor=? "
                                   "ORDER BY id DESC LIMIT ?", (actor, limit)).fetchall()
            return [dict(r) for r in reversed(rows)]

    def reserve_call(self, limit):
        day = self.now()[:10]
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                count = self.db.execute("SELECT COUNT(*) FROM calls WHERE day=?", (day,)).fetchone()[0]
                if count >= limit:
                    raise ValueError("Дневной лимит запросов исчерпан (день считается по UTC).")
                cursor = self.db.execute("INSERT INTO calls(day,result,at) VALUES (?,?,?)", (day, '{}', self.now()))
                self.db.commit()
                return cursor.lastrowid
            except Exception:
                self.db.rollback()
                raise

    def finish_call(self, call_id, result):
        with self.lock, self.db:
            self.db.execute("UPDATE calls SET result=? WHERE id=?", (json.dumps(result), call_id))

    def accept_update(self, update_id, chat_id, text, status="pending"):
        with self.lock, self.db:
            inserted = False
            # Outsiders must not be able to grow the private conversation database.
            if status != "ignored":
                cursor = self.db.execute("INSERT OR IGNORE INTO telegram_jobs(update_id,chat_id,text,status) "
                                         "VALUES (?,?,?,?)", (update_id, chat_id, text, status))
                inserted = cursor.rowcount == 1
            offset = max(update_id + 1, self.get("telegram_offset", 0))
            self.db.execute("INSERT OR REPLACE INTO settings VALUES ('telegram_offset',?)", (json.dumps(offset),))
            return inserted

    def jobs(self, status):
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM telegram_jobs WHERE status=? ORDER BY update_id", (status,))]

    def job_status(self, update_id, status, reply="", error="", revision=None):
        with self.lock, self.db:
            self.db.execute("UPDATE telegram_jobs SET status=?,reply=?,error=?,revision=? WHERE update_id=?",
                            (status, reply, error, revision, update_id))

    def cancel_pending(self):
        with self.lock, self.db:
            self.db.execute("UPDATE telegram_jobs SET status='cancelled' WHERE status='pending'")

    def channel_posts(self, limit=20):
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM channel_posts ORDER BY created_at DESC, rowid DESC LIMIT ?", (limit,))]

    def channel_post(self, post_id):
        with self.lock:
            row = self.db.execute("SELECT * FROM channel_posts WHERE id=?", (post_id,)).fetchone()
            return dict(row) if row else None

    def channel_attention(self):
        with self.lock:
            return [row[0] for row in self.db.execute(
                "SELECT id FROM channel_posts WHERE status IN ('publishing','uncertain') ORDER BY created_at")]

    def add_channel_post(self, post_id, payload, target=0):
        with self.lock, self.db:
            self.db.execute("INSERT INTO channel_posts(id,created_at,status,payload,target) VALUES (?,?,?,?,?)",
                            (post_id, self.now(), "draft", json.dumps(payload, ensure_ascii=False), target))

    def update_channel_post(self, post_id, **values):
        allowed = {"status", "photo", "photo_message_id", "text_message_id", "error", "published_at", "target"}
        if not values or not set(values) <= allowed:
            raise ValueError("Неверные поля публикации.")
        with self.lock, self.db:
            fields = ",".join(key + "=?" for key in values)
            self.db.execute("UPDATE channel_posts SET " + fields + " WHERE id=?", (*values.values(), post_id))

    def claim_channel_post(self, post_id, target):
        with self.lock, self.db:
            changed = self.db.execute("UPDATE channel_posts SET status='publishing',target=? "
                                      "WHERE id=? AND status='draft' AND target IN (0,?)",
                                      (target, post_id, target)).rowcount
            return bool(changed)

    def add_channel_event(self, event_id, text):
        with self.lock, self.db:
            self.db.execute("INSERT INTO channel_events(id,text,created_at) VALUES (?,?,?)",
                            (event_id, text, self.now()))

    def channel_events(self):
        with self.lock:
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM channel_events WHERE used_by='' ORDER BY created_at LIMIT 10")]

    def claim_channel_slot(self, slot):
        with self.lock, self.db:
            return bool(self.db.execute("INSERT OR IGNORE INTO channel_slots(slot,state,created_at) "
                                        "VALUES (?,'drafting',?)", (slot, self.now())).rowcount)

    def channel_slot(self, slot):
        with self.lock:
            row = self.db.execute("SELECT * FROM channel_slots WHERE slot=?", (slot,)).fetchone()
            return dict(row) if row else None

    def set_channel_slot(self, slot, post_id, state):
        with self.lock, self.db:
            self.db.execute("UPDATE channel_slots SET post_id=?,state=? WHERE slot=?", (post_id, state, slot))
