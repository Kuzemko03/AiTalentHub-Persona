"""Restart a failed Joker process with bounded backoff; logs stay in data/."""
import logging
import logging.handlers
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    folder = ROOT / "data" / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger("supervisor")
    log.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(folder / "runtime.log", maxBytes=2_000_000,
                                                   backupCount=3, encoding="utf-8")
    log.addHandler(handler)
    child = None
    attempts = 0
    try:
        while attempts < 5:
            started = time.monotonic()
            child = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "joker", "serve", *sys.argv[1:]],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace")
            for line in child.stdout:
                log.info(line.rstrip())
            code = child.wait()
            if code == 0:
                return
            attempts = 1 if time.monotonic() - started >= 300 else attempts + 1
            log.error("Joker exited: %s; restart %s/5", code, attempts)
            if attempts < 5:
                time.sleep(min(60, 2 ** attempts))
        log.error("Restart limit reached; check configuration before starting again.")
        raise SystemExit(1)
    except KeyboardInterrupt:
        if child and child.poll() is None:
            child.terminate()
            try:
                child.wait(8)
            except subprocess.TimeoutExpired:
                child.kill()


if __name__ == "__main__":
    main()
