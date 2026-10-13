"""Read B1's usage ledger (``usage.db``) without Row-Bot: counts and ids only, read-only.

The ledger never holds prompt or reply text, so reading it is safe even for a
validation data folder. It is opened read-only and never created.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .report import CallRecord, summarize_calls

_EVERYDAY = (".row-bot", ".thoth")


def everyday_data_folder(path: Path) -> bool:
    """Whether *path* is (inside) the person's everyday Row-Bot data folder."""
    resolved = Path(path).expanduser().resolve()
    for name in _EVERYDAY:
        root = (Path.home() / name).resolve()
        if resolved == root or resolved.is_relative_to(root):
            return True
    return False


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


def max_id(path: Path) -> int:
    """The newest row id, or 0 when the ledger does not exist yet."""
    if not Path(path).is_file():
        return 0
    with closing(_connect(path)) as connection:
        try:
            row = connection.execute("SELECT MAX(id) FROM model_usage").fetchone()
        except sqlite3.OperationalError:
            return 0
    return int(row[0] or 0)


def rows_after(path: Path, after_id: int = 0) -> list[CallRecord]:
    """Every call recorded after row *after_id*, in the order the calls started."""
    if not Path(path).is_file():
        return []
    with closing(_connect(path)) as connection:
        try:
            rows = connection.execute(
                "SELECT * FROM model_usage WHERE id > ? ORDER BY created_at, id", (int(after_id),)
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [CallRecord.from_row(dict(row)) for row in rows]


def summarize_folder(data_dir: Path, rates: Any = None) -> dict[str, Any]:
    """Per provider/model/purpose totals of a data folder's ledger (the ``ledger`` command)."""
    calls = rows_after(Path(data_dir) / "usage.db")
    groups: dict[tuple[str, str, str], list[CallRecord]] = {}
    for call in calls:
        groups.setdefault((call.provider, call.model, call.purpose), []).append(call)
    out = []
    for (provider, model, purpose), group in sorted(groups.items()):
        totals = summarize_calls(group, rates)
        out.append({
            "provider": provider,
            "model": model,
            "purpose": purpose,
            "calls": totals.calls,
            "calls_without_counts": totals.calls_without_counts,
            "input_tokens": totals.input_tokens,
            "cache_read_tokens": totals.cache_read_tokens,
            "cache_write_tokens": totals.cache_write_tokens,
            "cache_write_1h_tokens": totals.cache_write_1h_tokens,
            "output_tokens": totals.output_tokens,
            "reasoning_tokens": totals.reasoning_tokens,
            "cache_read_share": totals.cache_read_share,
            "cost_usd": totals.cost_usd,
            "ttft_ms_mean": totals.ttft_ms_mean,
        })
    return {"calls": len(calls), "groups": out}
