"""Single long-running instance and verifiable, private SQLite snapshots."""
import sqlite3
from contextlib import closing
from datetime import datetime, timezone


class InstanceLock:
    def __init__(self, root):
        self.path = root / "data" / "runtime.lock"
        self.file = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            import os
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise ValueError("Джокер уже запущен из этой папки. Открой существующую панель.") from None

    def close(self):
        if self.file:
            self.file.close()
            self.file = None


def snapshot(store, root):
    folder = root / "data" / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = folder / (stamp + ".sqlite3")
    with closing(sqlite3.connect(path)) as output:
        with store.lock:
            store.db.backup(output)
        if output.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Проверка копии SQLite не пройдена.")
    # Retain 14 local snapshots. Never touch live state or external directories.
    for old in sorted(folder.glob("*.sqlite3"))[:-14]:
        old.unlink()
    return path
