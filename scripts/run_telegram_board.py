#!/usr/bin/env python3
"""Run the Telegram board and auto-restart on source code changes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WATCH_DIRS = [PROJECT_ROOT / "src", PROJECT_ROOT / "scripts"]
POLL_SECONDS = float(os.getenv("TELEGRAM_BOARD_POLL_SECONDS", "1.5"))
RESTART_DELAY_SECONDS = float(os.getenv("TELEGRAM_BOARD_RESTART_DELAY_SECONDS", "0.75"))


def _source_snapshot() -> Dict[str, float]:
    snapshot: Dict[str, float] = {}
    for root in WATCH_DIRS:
        if not root.exists():
            continue
        for py_file in root.rglob("*.py"):
            if "__pycache__" in py_file.parts:
                continue
            snapshot[str(py_file)] = py_file.stat().st_mtime
    return snapshot


def _start_bot() -> subprocess.Popen[bytes]:
    command = [sys.executable, str(PROJECT_ROOT / "src/main.py"), "bot"]
    return subprocess.Popen(command, cwd=str(PROJECT_ROOT))


def _stop_bot(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGINT)
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def main() -> None:
    print("telegram board watcher: starting bot")
    last_snapshot = _source_snapshot()
    process = _start_bot()

    try:
        while True:
            time.sleep(POLL_SECONDS)
            if process.poll() is not None:
                print("telegram board watcher: bot process exited, restarting in 1s")
                process = _start_bot()
                time.sleep(RESTART_DELAY_SECONDS)
                last_snapshot = _source_snapshot()
                continue

            snapshot = _source_snapshot()
            if snapshot != last_snapshot:
                print("telegram board watcher: detected source change, restarting bot")
                _stop_bot(process)
                time.sleep(RESTART_DELAY_SECONDS)
                process = _start_bot()
                last_snapshot = _source_snapshot()
    except KeyboardInterrupt:
        print("telegram board watcher: shutdown requested")
        _stop_bot(process)


if __name__ == "__main__":
    main()
