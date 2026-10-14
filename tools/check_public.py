"""Check the staged/tracked publication set, never print secret values."""
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
files = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
patterns = [re.compile(rb"\b\d{8,12}:[A-Za-z0-9_-]{30,50}\b"),
            re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{25,}\b"),
            re.compile(rb"\bsk-[A-Za-z0-9_-]{25,}\b")]
bad = []
for name in filter(None, files):
    path = Path(name)
    if (path.name == ".env" or name.startswith("data/") or name.endswith((".sqlite3", ".log"))
            or name.startswith("agent/joker/memory/entities/") and path.name != ".gitkeep"):
        bad.append(name + ": local runtime file")
        continue
    content = subprocess.check_output(["git", "show", ":" + name], cwd=ROOT)
    if any(pattern.search(content) for pattern in patterns):
        bad.append(name + ": credential-like value")
if not any(files):
    raise SystemExit("No tracked files to inspect; run git add with explicit paths first.")
for item in bad:
    print(item)
if bad:
    raise SystemExit(1)
print(f"Publication set checked: {len(list(filter(None, files)))} files; no runtime files or credential patterns.")
