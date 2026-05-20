"""Tests for finding H2 — trading-day timezone for daily PnL buckets.

The journal stores ``received_at`` in UTC. The day-rollover boundary
for ``daily_pnl`` and ``count_today`` used to be a hard-coded UTC date.
With ``TRADING_DAY_TIMEZONE`` set, the journal computes the bucket in
the operator's local tz so an ES/NQ trader's day-end matches the
session close, not 02:00 UTC mid-session.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from app.journal import Journal


def test_default_journal_uses_utc(tmp_path: Path):
    j = Journal(tmp_path / "j.db")
    assert j.trading_day_timezone == "UTC"
    # Today's date in UTC equals what _today_iso reports.
    expected = datetime.now(timezone.utc).date().isoformat()
    assert j._today_iso() == expected


def test_journal_honors_configured_timezone(tmp_path: Path):
    j = Journal(tmp_path / "j.db", trading_day_timezone="America/New_York")
    assert j.trading_day_timezone == "America/New_York"
    # The reported date matches ZoneInfo("America/New_York") locally.
    expected = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    assert j._today_iso() == expected


def test_today_iso_at_02_utc_falls_on_prior_local_day(tmp_path: Path):
    """At 02:00 UTC, an America/New_York trader's day hasn't rolled."""
    j = Journal(tmp_path / "j.db", trading_day_timezone="America/New_York")
    # 2026-05-21 02:00 UTC = 2026-05-20 22:00 EDT (UTC-4 in May).
    fixed_utc = datetime(2026, 5, 21, 2, 0, tzinfo=timezone.utc)

    class _StubDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is None:
                return fixed_utc.replace(tzinfo=None)
            return fixed_utc.astimezone(tz)

    with patch("app.journal.datetime", _StubDatetime):
        assert j._today_iso() == "2026-05-20"


def test_daily_pnl_bucketed_in_local_day(tmp_path: Path):
    """A loss recorded at 02:00 UTC on day N must hit the local-day-N-1
    bucket when the tz is America/New_York."""
    j = Journal(tmp_path / "j.db", trading_day_timezone="America/New_York")
    fixed_utc = datetime(2026, 5, 21, 2, 0, tzinfo=timezone.utc)

    class _StubDatetime(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            if tz is None:
                return fixed_utc.replace(tzinfo=None)
            return fixed_utc.astimezone(tz)

    with patch("app.journal.datetime", _StubDatetime):
        j.add_daily_pnl(-50.0)
        # Same wall-clock instant — the bucket the read uses must match
        # the bucket the write used.
        assert j.get_daily_pnl() == -50.0
        # Explicit lookup against the LOCAL date confirms the bucket key.
        assert j.get_daily_pnl(trade_date="2026-05-20") == -50.0
        # The "next day" UTC bucket must be empty.
        assert j.get_daily_pnl(trade_date="2026-05-21") == 0.0


def test_invalid_timezone_falls_back_to_utc_with_warning(
    tmp_path: Path, caplog
):
    with caplog.at_level(logging.WARNING, logger="signalbridge.journal"):
        j = Journal(
            tmp_path / "j.db", trading_day_timezone="Not/AReal_Zone"
        )
    # Still functional — falls back to UTC silently for the user, loudly
    # in the logs.
    expected = datetime.now(timezone.utc).date().isoformat()
    assert j._today_iso() == expected
    assert any(
        "TRADING_DAY_TIMEZONE" in record.message
        and record.levelno == logging.WARNING
        for record in caplog.records
    )


def test_settings_picks_up_trading_day_timezone(monkeypatch):
    """End-to-end: env var → Settings → constructor parameter."""
    monkeypatch.setenv("TRADING_DAY_TIMEZONE", "America/New_York")
    import sys

    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]
    from app import config as config_mod  # noqa: E402

    config_mod.get_settings.cache_clear()
    s = config_mod.get_settings()
    assert s.trading_day_timezone == "America/New_York"
