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
from typing import Any, Dict, Iterable, Optional

log = logging.getLogger("signalbridge.symbol_map")


# Provider columns the UI knows how to edit.
KNOWN_PROVIDERS: tuple[str, ...] = ("paper", "topstep", "tradovate")


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

    def reload(self) -> None:
        """Re-read the underlying file. Safe to call after a save from the UI."""
        self._map = {}
        self._load()

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

    def resolve_explicit(
        self, ticker: Optional[str], provider: str
    ) -> Optional[str]:
        """Return the broker-specific symbol only when explicitly mapped.

        Differs from ``resolve()`` in that it returns ``None`` rather than
        falling back to the TradingView ticker. The Topstep order builder
        uses this to refuse silently routing a guessed contract id —
        ProjectX expects real contract ids (e.g. ``CON.F.US.MES.M26``,
        not just ``MES``), so a missing mapping must surface as a
        ``symbol_mapping_missing`` rejection rather than a fabricated id.
        """
        if not ticker:
            return None
        entry = self._map.get(ticker)
        if isinstance(entry, dict):
            mapped = entry.get(provider)
            if isinstance(mapped, str) and mapped:
                return mapped
        return None

    # ------------------------------------------------------------------
    # UI helpers — used by /settings/symbols
    # ------------------------------------------------------------------

    def all_mappings(self) -> Dict[str, Dict[str, str]]:
        """Snapshot of the current mappings (excluding metadata keys).

        The on-disk file may carry sentinel keys like ``_comment`` /
        ``_warning`` so operators can leave themselves notes. Those are
        preserved by ``replace_all`` but filtered out here so the UI
        doesn't display them as rows.
        """
        out: Dict[str, Dict[str, str]] = {}
        for ticker, entry in self._map.items():
            if ticker.startswith("_"):
                continue
            if not isinstance(entry, dict):
                continue
            row: Dict[str, str] = {}
            for provider in KNOWN_PROVIDERS:
                value = entry.get(provider, "")
                row[provider] = str(value) if isinstance(value, str) else ""
            out[ticker] = row
        return out

    def replace_all(self, mappings: Dict[str, Dict[str, str]]) -> None:
        """Replace the on-disk mapping with ``mappings``. Preserves any
        underscore-prefixed metadata keys already in the file."""
        normalized: Dict[str, Any] = {}
        # Carry forward metadata keys (``_comment`` / ``_warning``).
        for key, value in self._map.items():
            if key.startswith("_"):
                normalized[key] = value
        for ticker, row in mappings.items():
            if not ticker:
                continue
            cleaned: Dict[str, str] = {}
            for provider in KNOWN_PROVIDERS:
                value = (row or {}).get(provider, "")
                cleaned[provider] = str(value or "").strip()
            normalized[ticker] = cleaned
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(normalized, indent=2) + "\n")
        self._map = normalized


def parse_form_mappings(
    tickers: Iterable[str],
    paper_values: Iterable[str],
    topstep_values: Iterable[str],
    tradovate_values: Iterable[str],
) -> Dict[str, Dict[str, str]]:
    """Turn parallel form arrays into a normalized mapping dict.

    Validation rules:
      * TradingView ticker is required (rows with a blank ticker are dropped).
      * Paper symbol defaults to the ticker when blank.
      * Topstep / Tradovate symbols may be blank.

    Raises ``ValueError`` when a row is malformed beyond a blank ticker.
    """
    tickers = list(tickers)
    paper_values = list(paper_values)
    topstep_values = list(topstep_values)
    tradovate_values = list(tradovate_values)

    length = len(tickers)
    if not (
        len(paper_values) == length
        and len(topstep_values) == length
        and len(tradovate_values) == length
    ):
        raise ValueError("symbol form arrays are mis-aligned")

    out: Dict[str, Dict[str, str]] = {}
    for idx in range(length):
        ticker = (tickers[idx] or "").strip()
        if not ticker:
            continue
        paper = (paper_values[idx] or "").strip() or ticker
        topstep = (topstep_values[idx] or "").strip()
        tradovate = (tradovate_values[idx] or "").strip()
        out[ticker] = {
            "paper": paper,
            "topstep": topstep,
            "tradovate": tradovate,
        }
    return out
