"""Simple in-process kill switch state.

Persistence is intentionally minimal — a single sentinel file alongside
the database directory. This keeps the API small for a single-user bot.
"""
from __future__ import annotations

import threading
from pathlib import Path


class KillSwitch:
    def __init__(self, sentinel_path: str | Path, *, enabled: bool = True) -> None:
        self.sentinel_path = Path(sentinel_path)
        self.enabled = enabled
        self._lock = threading.Lock()
        self.sentinel_path.parent.mkdir(parents=True, exist_ok=True)

    def is_active(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            return self.sentinel_path.exists()

    def activate(self, reason: str = "") -> None:
        with self._lock:
            self.sentinel_path.write_text(reason or "active")

    def deactivate(self) -> None:
        with self._lock:
            if self.sentinel_path.exists():
                self.sentinel_path.unlink()
