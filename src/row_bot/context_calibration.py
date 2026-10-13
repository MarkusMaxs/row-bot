"""Calibrated request-size estimates (context and cost plan, WP A4b).

Row-Bot sizes a request before sending it with a fast approximation of about four characters a
token (LangChain's ``count_tokens_approximately``). Real tokenizers differ by model and by text:
Claude reads English at about 3.5 characters a token, code and JSON cost more, and CJK text can
cost a token per character. Compaction now starts near the limit (``models.context_reserves``), so
an estimate that undercounts would overflow instead of compacting.

Each (provider, model) therefore keeps a factor: an exponential moving average of the input tokens
the provider reported over Row-Bot's estimate of the same request. Estimates are multiplied by it
before they are compared with the compaction point or the usable input. The factor:

* is learned only from requests large enough to measure (``MIN_ESTIMATE_TOKENS``) whose ratio is
  plausible, and bounded to ``FACTOR_BOUNDS``;
* is raised by a provider's context-overflow error, which shows the request was at least as large
  as the window (``observe_overflow``);
* is never learned on local Ollama, whose ``prompt_eval_count`` counts only the tokens it had not
  cached, so it says nothing about the request's size.

Factors live in ``usage.db`` next to the usage ledger: a provider id, a model reference, a number
and a count. Nothing about a conversation. Reads never create the file and writes never raise.
"""
from __future__ import annotations

import logging
import math
import re
import sqlite3
import threading
import time
from contextlib import closing

from row_bot.data_paths import get_usage_db_path

logger = logging.getLogger(__name__)

FACTOR_BOUNDS = (0.5, 3.0)
# A sample outside this range is a broken count (a cached-only count, a fake), not a tokenizer.
PLAUSIBLE_RATIO = (0.25, 8.0)
MIN_ESTIMATE_TOKENS = 1_000
EMA_ALPHA = 0.2
# Providers whose reported input is not the request's size (Ollama counts only uncached tokens).
_UNCALIBRATED_PROVIDERS = ("ollama", "local")
# After an overflow the factor puts the request this far past the window it overflowed.
_OVERFLOW_MARGIN = 1.02

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS context_calibration ("
    "provider TEXT NOT NULL, "
    "model TEXT NOT NULL, "
    "factor REAL NOT NULL, "
    "samples INTEGER NOT NULL, "
    "updated_at REAL NOT NULL, "
    "PRIMARY KEY (provider, model))"
)

_lock = threading.Lock()
# (usage.db path, provider, model) -> (factor, samples); None when the file has no row.
_cache: dict[tuple[str, str, str], tuple[float, int] | None] = {}
_written_since_reset = True  # a previous run may have left rows in a test data folder

# What providers say when a request overflows, with the request's own size in it.
_REPORTED_INPUT_PATTERNS = tuple(re.compile(pattern, re.IGNORECASE) for pattern in (
    r"prompt is too long:\s*([\d,]+)\s*tokens",  # Anthropic
    r"\(([\d,]+)\s+in the messages",  # OpenAI chat completions
    r"messages resulted in\s+([\d,]+)\s+tokens",  # OpenAI
    r"requested\s+([\d,]+)\s+tokens in the messages",  # vLLM
    r"([\d,]+)\s+of text input",  # OpenRouter
    r"input token count \(([\d,]+)\)",  # Gemini
    r"request \(([\d,]+)\s+tokens\)",  # llama.cpp
))


def _key(provider_id: str | None, model_ref: str | None) -> tuple[str, str]:
    return str(provider_id or "").strip().lower()[:128], str(model_ref or "").strip()[:256]


def _calibrated_provider(provider: str) -> bool:
    return bool(provider) and not provider.startswith(_UNCALIBRATED_PROVIDERS)


def _bounded(value: float) -> float:
    low, high = FACTOR_BOUNDS
    return min(high, max(low, float(value)))


def _connect(*, create: bool) -> sqlite3.Connection | None:
    path = get_usage_db_path(create_parent=create)
    if not create and not path.is_file():
        return None
    connection = sqlite3.connect(str(path), timeout=5)
    connection.execute(_SCHEMA)
    return connection


def _stored(provider: str, model: str) -> tuple[float, int] | None:
    cache_key = (str(get_usage_db_path(create_parent=False)), provider, model)
    with _lock:
        if cache_key in _cache:
            return _cache[cache_key]
    stored: tuple[float, int] | None = None
    try:
        connection = _connect(create=False)
        if connection is not None:
            with closing(connection), connection:
                row = connection.execute(
                    "SELECT factor, samples FROM context_calibration WHERE provider = ? AND model = ?",
                    (provider, model),
                ).fetchone()
            if row and isinstance(row[0], (int, float)) and math.isfinite(row[0]):
                stored = (_bounded(row[0]), max(0, int(row[1] or 0)))
    except Exception:
        logger.debug("Context calibration unavailable", exc_info=True)
    with _lock:
        _cache[cache_key] = stored
    return stored


def _save(provider: str, model: str, factor: float, samples: int) -> None:
    global _written_since_reset
    cache_key = (str(get_usage_db_path(create_parent=False)), provider, model)
    with _lock:
        _cache[cache_key] = (factor, samples)
        _written_since_reset = True
    try:
        connection = _connect(create=True)
        if connection is None:
            return
        with closing(connection), connection:
            connection.execute(
                "INSERT INTO context_calibration (provider, model, factor, samples, updated_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(provider, model) DO UPDATE SET "
                "factor = excluded.factor, samples = excluded.samples, updated_at = excluded.updated_at",
                (provider, model, factor, samples, time.time()),
            )
    except Exception:
        logger.debug("Context calibration was not saved", exc_info=True)


def factor(provider_id: str | None, model_ref: str | None) -> float:
    """How many real tokens one estimated token is for this model (1.0 until measured)."""
    provider, model = _key(provider_id, model_ref)
    if not _calibrated_provider(provider) or not model:
        return 1.0
    stored = _stored(provider, model)
    return stored[0] if stored else 1.0


def calibrated(tokens: int, provider_id: str | None, model_ref: str | None) -> int:
    """*tokens* estimated for a request to this model, as the provider is expected to count them."""
    scale = factor(provider_id, model_ref)
    value = max(0, int(tokens or 0))
    return value if scale == 1.0 else int(math.ceil(value * scale))


def uncalibrated(tokens: int, provider_id: str | None, model_ref: str | None) -> int:
    """The estimate that calibrates to *tokens* (a budget in the provider's tokens, in estimated ones)."""
    return max(0, int(int(tokens or 0) / factor(provider_id, model_ref)))


def observe(
    provider_id: str | None,
    model_ref: str | None,
    *,
    estimated: int | None,
    reported: int | None,
) -> float | None:
    """Learn from one request: Row-Bot's estimate against the input the provider reported.

    Returns the new factor, or None when the sample says nothing (too small, implausible, local).
    """
    provider, model = _key(provider_id, model_ref)
    if not _calibrated_provider(provider) or not model:
        return None
    try:
        estimate, actual = int(estimated or 0), int(reported or 0)
    except (TypeError, ValueError):
        return None
    if estimate < MIN_ESTIMATE_TOKENS or actual <= 0:
        return None
    ratio = actual / estimate
    if not PLAUSIBLE_RATIO[0] <= ratio <= PLAUSIBLE_RATIO[1]:
        logger.debug("Context calibration: ignored ratio %.2f for %s %s", ratio, provider, model)
        return None
    previous = _stored(provider, model)
    if previous is None:
        updated, samples = _bounded(ratio), 1
    else:
        current, count = previous
        alpha = max(EMA_ALPHA, 1.0 / (count + 1))
        updated, samples = _bounded(current + alpha * (_bounded(ratio) - current)), count + 1
    _save(provider, model, updated, samples)
    return updated


def reported_input_tokens(error_text: str) -> int | None:
    """The request size a provider's context-overflow error states, when it states one."""
    text = str(error_text or "")
    for pattern in _REPORTED_INPUT_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                value = int(match.group(1).replace(",", ""))
            except ValueError:
                continue
            if value > 0:
                return value
    return None


def observe_overflow(
    provider_id: str | None,
    model_ref: str | None,
    *,
    estimated: int | None,
    limit: int | None,
    error_text: str = "",
) -> float | None:
    """Learn from a context-overflow error: the request was at least as large as the window.

    The size the error states is an ordinary sample. Without one, the factor is raised so that the
    rejected request would have counted just past *limit*. Returns the new factor, or None.
    """
    provider, model = _key(provider_id, model_ref)
    if not _calibrated_provider(provider) or not model:
        return None
    reported = reported_input_tokens(error_text)
    if reported is not None:
        return observe(provider_id, model_ref, estimated=estimated, reported=reported)
    try:
        estimate, window = int(estimated or 0), int(limit or 0)
    except (TypeError, ValueError):
        return None
    if estimate <= 0 or window <= 0:
        return None
    needed = window * _OVERFLOW_MARGIN / estimate
    previous = _stored(provider, model)
    current, count = previous if previous else (1.0, 0)
    if current >= needed:
        return current
    updated = _bounded(needed)
    _save(provider, model, updated, count + 1)
    return updated


def reset_for_tests() -> None:
    """Forget every factor (and those a previous test or run saved in the current data folder)."""
    global _written_since_reset
    with _lock:
        _cache.clear()
        stale = _written_since_reset
        _written_since_reset = False
    if not stale:
        return
    try:
        connection = _connect(create=False)
        if connection is not None:
            with closing(connection), connection:
                connection.execute("DELETE FROM context_calibration")
    except Exception:
        logger.debug("Context calibration reset skipped", exc_info=True)
