"""Second-host half of farm_storage_spike.py; use an interactive test account."""

import json
import os
import sys
import time
from pathlib import Path

from farm_storage_spike import atomic_json, share_lock, wait_file


root = Path(sys.argv[1])
try:
    with share_lock(root):
        atomic_json(root / "control.json", {"generation": 1, "check": "1" * 128})
        atomic_json(root / "remote-ready.json", {"host": os.environ["COMPUTERNAME"], "pid": os.getpid()})
        wait_file(root / "release-remote")
    for generation in range(2, 102):
        with share_lock(root):
            atomic_json(root / "control.json", {"generation": generation, "check": str(generation) * 128})
        time.sleep(0.01)
    (root / "remote-writer-done").touch()
    with share_lock(root):
        (root / "remote-death-lock-held").touch()
        wait_file(root / "kill-remote")
        os._exit(23)
except Exception as exc:
    (root / "remote-error.txt").write_text(str(exc), encoding="utf-8")
    raise
