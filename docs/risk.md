# Risk sizing

SignalBridge accepts trade quantity in one of two modes. The mode is set
from the Risk Settings page (`/settings/risk`) and persisted in SQLite.

## Strategy-managed risk (default)

`STRATEGY_MANAGED_RISK=true` (the dashboard toggle
**"Risk settings are set by the strategy"** is **on**).

* SignalBridge uses the `contracts` value from the TradingView alert.
* The strategy on TradingView is the single source of truth for trade
  size.
* If the alert is missing `contracts`, sends 0, or sends a non-numeric
  value, the webhook is rejected with
  `missing_or_invalid_alert_contracts`.
* If the alert quantity is greater than `MAX_CONTRACTS_PER_TRADE`, the
  webhook is rejected with `contracts_above_max`.

## Fixed contract size

`STRATEGY_MANAGED_RISK=false` (toggle **off**).

* SignalBridge **ignores** the alert's `contracts` field for execution
  sizing.
* Every accepted signal trades `FIXED_CONTRACTS_PER_TRADE` contracts.
* The log line `alert contracts ignored; fixed sizing used: alert=…
  fixed=…` records both numbers.
* The journal entry's `execution_result.risk_sizing` carries
  `alert_contracts`, `executed_contracts`, and `strategy_managed_risk`
  for full auditability.
* You cannot save `FIXED_CONTRACTS_PER_TRADE > MAX_CONTRACTS_PER_TRADE`
  through the dashboard — the form rejects the change.

## Hard safety cap

`MAX_CONTRACTS_PER_TRADE` is always enforced after sizing, no matter the
mode. A `contracts_above_max` rejection is always possible if a misbehaving
strategy or a typo in fixed sizing slips through.

## Recommended default

Keep `STRATEGY_MANAGED_RISK=true` so position sizing lives with the
strategy code on TradingView (where backtests already validate it), and
set `MAX_CONTRACTS_PER_TRADE` conservatively as a defensive ceiling.

## Example behavior

| Mode               | alert.contracts | fixed | max | Result              |
|--------------------|-----------------|-------|-----|---------------------|
| strategy-managed   | 2               | (—)   | 3   | execute 2           |
| strategy-managed   | 5               | (—)   | 3   | reject contracts_above_max |
| fixed              | 5               | 1     | 3   | execute 1 (alert ignored)  |
| fixed              | missing         | 1     | 3   | execute 1 (alert ignored)  |
| strategy-managed   | missing         | (—)   | 3   | reject missing_or_invalid_alert_contracts |
