"""What a model call cost: the provider's own figure, or the person's rates file.

There is no built-in price table. A call is priced from the cost the provider
returned (OpenRouter), else from a rates file the person points
``ROW_BOT_LIVE_EVAL_RATES`` at, else it is "unpriced". Rates are US dollars per
million tokens::

    {
      "models": {
        "anthropic:claude-sonnet-5-5": {"input": 0, "output": 0, "cache_read": 0,
                                        "cache_write_5m": 0, "cache_write_1h": 0},
        "openai:*": {"input": 0, "output": 0, "cache_read": 0}
      }
    }

Keys are ``provider:model``, a bare ``model``, or ``provider:*``. A rate a call
needs but the file leaves out makes that call unpriced, never guessed.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

RATES_ENV = "ROW_BOT_LIVE_EVAL_RATES"
_RATE_FIELDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


@dataclass(frozen=True)
class Rates:
    """US dollars per million tokens; None where the file gives no rate."""

    input: float | None = None
    output: float | None = None
    cache_read: float | None = None
    cache_write_5m: float | None = None
    cache_write_1h: float | None = None


@dataclass(frozen=True)
class CallTokens:
    """One call's counts as the usage ledger stores them (B1 meanings).

    ``input`` includes cache reads and writes; ``output`` includes reasoning;
    ``cache_write`` includes the 1-hour writes. None means "not reported".
    """

    input: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None
    cache_write_1h: int | None = None
    output: int | None = None
    reasoning: int | None = None
    cost: float | None = None

    @property
    def processed(self) -> int:
        """Tokens the provider processed for this call: input plus output."""
        return int(self.input or 0) + int(self.output or 0)


@dataclass(frozen=True)
class Price:
    """A priced amount, where it came from, and why it is missing when it is."""

    usd: float | None
    source: str  # "provider" | "rates" | "unpriced"
    reason: str = ""


def _rate(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    return rate if rate >= 0 else None


def parse_rates(document: Mapping[str, Any]) -> dict[str, Rates]:
    """The ``models`` table of a rates document, keyed in lower case."""
    models = document.get("models") if isinstance(document, Mapping) else None
    table: dict[str, Rates] = {}
    if not isinstance(models, Mapping):
        return table
    for key, entry in models.items():
        if not isinstance(entry, Mapping):
            continue
        table[str(key).strip().lower()] = Rates(**{name: _rate(entry.get(name)) for name in _RATE_FIELDS})
    return table


def load_rates(path: str | os.PathLike[str] | None = None) -> dict[str, Rates]:
    """The person's rates file (``ROW_BOT_LIVE_EVAL_RATES``), or an empty table."""
    location = path if path is not None else os.environ.get(RATES_ENV, "").strip()
    if not location:
        return {}
    return parse_rates(json.loads(Path(location).read_text(encoding="utf-8")))


def rates_for(table: Mapping[str, Rates], provider: str, model: str) -> Rates | None:
    """The most specific rates entry for a provider's model."""
    provider_key = str(provider or "").strip().lower()
    model_key = str(model or "").strip().lower()
    for key in (f"{provider_key}:{model_key}", model_key, f"{provider_key}:*"):
        if key and key in table:
            return table[key]
    return None


def price_call(tokens: CallTokens, rates: Rates | None) -> Price:
    """One call's cost: the provider's figure first, then the rates, else unpriced."""
    if tokens.cost is not None:
        return Price(float(tokens.cost), "provider")
    if tokens.input is None and tokens.output is None:
        return Price(None, "unpriced", "the provider reported no counts")
    if rates is None:
        return Price(None, "unpriced", "no rates for this model")
    cache_read = int(tokens.cache_read or 0)
    cache_write = int(tokens.cache_write or 0)
    write_1h = min(int(tokens.cache_write_1h or 0), cache_write)
    parts = {
        "input": max(0, int(tokens.input or 0) - cache_read - cache_write),
        "cache_read": cache_read,
        "cache_write_5m": cache_write - write_1h,
        "cache_write_1h": write_1h,
        "output": int(tokens.output or 0),
    }
    usd = 0.0
    for name, count in parts.items():
        if not count:
            continue
        rate = getattr(rates, name)
        if rate is None:
            return Price(None, "unpriced", f"no {name} rate")
        usd += count * rate / 1_000_000
    return Price(usd, "rates")


def upper_bound_usd(input_tokens: int, output_tokens: int, rates: Rates | None) -> float | None:
    """A ceiling for a call that has not run yet: every input token at the dearest input rate.

    Cache writes can cost more than plain input (a 1-hour write is about twice
    the input rate), so the ceiling takes the highest input-side rate given.
    """
    if rates is None or rates.input is None or rates.output is None:
        return None
    input_rate = max(rate for rate in (rates.input, rates.cache_write_5m, rates.cache_write_1h) if rate is not None)
    return (max(0, input_tokens) * input_rate + max(0, output_tokens) * rates.output) / 1_000_000
