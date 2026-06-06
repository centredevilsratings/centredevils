"""
Daily USD spend cap for the Sonnet 4.6 drafter.

Tracks every Claude API call's token usage in SQLite, keyed by UTC date.
Before each draft call, the caller asks `under_budget()` — if today's
estimated USD spend is at or above the cap, the call is skipped entirely
and the draft is dropped. After every call that DID fire, the caller
passes the response's `usage` object to `record_usage()` so the running
total is up to date.

Cap is set via SONNET_DAILY_CAP_USD env var (default 10.0). Sonnet 4.6
pricing baked in below — update if Anthropic ever changes it.

Pricing (Sonnet 4.6, per 1M tokens):
  input             $ 3.00
  output            $15.00
  cache create (5m) $ 3.75  (1.25x input)
  cache read        $ 0.30  (0.10x input)
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import sqlite3
import threading

log = logging.getLogger("football-bot.budget")

DAILY_CAP_USD = float(os.environ.get("SONNET_DAILY_CAP_USD", "10.0"))
DB_PATH = os.environ.get("DB_PATH", "/data/football_ops_v2.db")

_PRICE_INPUT = 3.00 / 1_000_000
_PRICE_OUTPUT = 15.00 / 1_000_000
_PRICE_CACHE_WRITE = 3.75 / 1_000_000
_PRICE_CACHE_READ = 0.30 / 1_000_000

_LOCK = threading.Lock()


def _today() -> str:
    return _dt.datetime.utcnow().strftime("%Y-%m-%d")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.execute(
        "CREATE TABLE IF NOT EXISTS sonnet_spend("
        " day TEXT PRIMARY KEY,"
        " input_tokens INTEGER NOT NULL DEFAULT 0,"
        " output_tokens INTEGER NOT NULL DEFAULT 0,"
        " cache_creation_tokens INTEGER NOT NULL DEFAULT 0,"
        " cache_read_tokens INTEGER NOT NULL DEFAULT 0,"
        " usd_spent REAL NOT NULL DEFAULT 0.0,"
        " calls INTEGER NOT NULL DEFAULT 0)"
    )
    return c


def _cost(input_t: int, output_t: int, cache_create_t: int, cache_read_t: int) -> float:
    return (
        input_t * _PRICE_INPUT
        + output_t * _PRICE_OUTPUT
        + cache_create_t * _PRICE_CACHE_WRITE
        + cache_read_t * _PRICE_CACHE_READ
    )


def daily_spend_usd() -> float:
    """Return today's UTC-day Sonnet spend in USD (0.0 if nothing yet)."""
    with _LOCK:
        with _conn() as c:
            row = c.execute(
                "SELECT usd_spent FROM sonnet_spend WHERE day=?",
                (_today(),),
            ).fetchone()
    return float(row[0]) if row else 0.0


def under_budget() -> tuple[bool, float]:
    """(ok_to_call, spent_usd). ok_to_call is False once we're at/over cap."""
    spent = daily_spend_usd()
    return (spent < DAILY_CAP_USD, spent)


def record_usage(usage) -> float:
    """Record one Claude call's usage on today's row. Returns new total USD.

    `usage` is the response.usage object from the anthropic SDK — has
    `input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
    `cache_read_input_tokens`. Missing/None fields treated as 0.
    """
    inp = int(getattr(usage, "input_tokens", 0) or 0)
    out = int(getattr(usage, "output_tokens", 0) or 0)
    cwrite = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
    cread = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
    delta = _cost(inp, out, cwrite, cread)
    day = _today()
    with _LOCK:
        with _conn() as c:
            c.execute(
                "INSERT INTO sonnet_spend(day, input_tokens, output_tokens, "
                "cache_creation_tokens, cache_read_tokens, usd_spent, calls) "
                "VALUES(?,?,?,?,?,?,1) "
                "ON CONFLICT(day) DO UPDATE SET "
                " input_tokens=input_tokens+excluded.input_tokens,"
                " output_tokens=output_tokens+excluded.output_tokens,"
                " cache_creation_tokens=cache_creation_tokens"
                "  +excluded.cache_creation_tokens,"
                " cache_read_tokens=cache_read_tokens+excluded.cache_read_tokens,"
                " usd_spent=usd_spent+excluded.usd_spent,"
                " calls=calls+1",
                (day, inp, out, cwrite, cread, delta),
            )
            total = c.execute(
                "SELECT usd_spent FROM sonnet_spend WHERE day=?", (day,)
            ).fetchone()[0]
    return float(total)
