"""Standalone Topstep auth probe.

Loads the exact same settings SignalBridge uses (env + SQLite overrides)
and calls ProjectX ``/api/Auth/loginKey`` through the production
``TopstepBroker.authenticate()`` code path. If auth succeeds it follows
up with ``/api/Account/search`` and prints the active accounts.

Run:
    python scripts/test_topstep_auth.py

Never prints the full API key or token — only lengths / last-4 previews.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings  # noqa: E402
from app.execution.topstep import TopstepBroker  # noqa: E402
from app.settings_store import SettingsStore  # noqa: E402
from app.signal_router import _topstep_token_sink  # noqa: E402


def _mask_tail(value: str, *, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "configured"
    return f"…{value[-keep:]}"


def main() -> int:
    settings = get_settings()
    store = SettingsStore(settings.database_abs_path)
    store.initialize_settings_from_env(settings)

    username = (settings.topstep_username or "").strip()
    api_key = (settings.topstep_api_key or "").strip()
    base_url = (settings.topstep_base_url or "").strip()
    account_id = (settings.topstep_account_id or "").strip()

    print("=== Topstep auth probe ===")
    print(f"base_url    : {base_url}")
    print(f"username    : {username!r}")
    print(f"username_len: {len(username)}")
    print(f"api_key_len : {len(api_key)}")
    print(f"api_key_tail: {_mask_tail(api_key)}")
    print(f"account_id  : {account_id!r}")
    print()

    if not username or not api_key:
        print("ERROR: TOPSTEP_USERNAME / TOPSTEP_API_KEY not configured.")
        print("Set them in .env or via /settings/broker and try again.")
        return 2

    broker = TopstepBroker(
        username=username,
        api_key=api_key,
        account_id=account_id,
        env=settings.topstep_env,
        base_url=base_url,
        ws_url=settings.topstep_ws_url,
        token=settings.topstep_token,
        token_expires_at=settings.topstep_token_expires_at,
        token_sink=_topstep_token_sink(settings, store),
    )

    print("--- POST /api/Auth/loginKey ---")
    auth = broker.authenticate()
    print(f"ok           : {auth.get('ok')}")
    print(f"status       : {auth.get('status')}")
    print(f"http_status  : {auth.get('http_status')}")
    print(f"errorCode    : {auth.get('error_code')}")
    print(f"errorMessage : {auth.get('error_message')}")
    print(f"message      : {auth.get('message')}")
    if auth.get("ok"):
        print(f"token_tail   : {_mask_tail(broker.token)}")
        print(f"expires_at   : {broker.token_expires_at}")
    print()

    if not auth.get("ok"):
        print("Auth failed — not calling /api/Account/search.")
        return 1

    print("--- POST /api/Account/search ---")
    accounts_resp = broker.get_accounts()
    print(f"ok      : {accounts_resp.get('ok')}")
    print(f"status  : {accounts_resp.get('status')}")
    print(f"message : {accounts_resp.get('message')}")
    accounts = accounts_resp.get("accounts") or []
    print(f"count   : {len(accounts)}")
    print()

    for acct in accounts:
        print(
            f"  id={acct.get('id')!r:>20}  "
            f"name={acct.get('name')!r:<24}  "
            f"balance={acct.get('balance')!r:<12}  "
            f"canTrade={acct.get('can_trade')!s:<5}  "
            f"isVisible={acct.get('is_visible')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
