"""Provider-aware TradingView -> broker symbol mapper.

Loads `config/symbols.json` (or whichever path `SYMBOLS_MAP_PATH` points
at) if it exists. The expected shape is provider-aware:

    {
      "MES1!": {
        "paper": "MES1!",
        "topstep": "MES",
        "tradovate": "MESM26"
      }
    }

If the file is missing, malformed, or doesn't contain a mapping for a
given (ticker, provider) pair, `resolve()` returns the original ticker
unchanged. This keeps SignalBridge usable on day one with paper alone.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("signalbridge.symbol_map")


class SymbolMap:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._map: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            log.warning("could not load symbol map at %s: %s", self.path, exc)
            return
        if isinstance(data, dict):
            self._map = data

    def resolve(self, ticker: Optional[str], provider: str) -> Optional[str]:
        """Return the broker-specific symbol for `(ticker, provider)`.

        Falls back to the original ticker if no mapping is configured.
        """
        if not ticker:
            return ticker
        entry = self._map.get(ticker)
        if isinstance(entry, dict):
            mapped = entry.get(provider)
            if isinstance(mapped, str) and mapped:
                return mapped
        return ticker
