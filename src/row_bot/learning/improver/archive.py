"""The archive of learning methods (L4a, D15): every strategy kept with its parent and its trial results.

- Each strategy is a ``strategy`` :class:`~row_bot.learning.contracts.Release` (its spec in a blob, its parents
  in ``parent_ids``): ``in_use`` (the current method), ``retired`` (was in use), ``candidate`` (being tried) or
  ``not_used`` (tried and not better). Nothing is ever deleted except by "Delete learning data".
- The **chain** is the order methods were in use (the first, each trial's switch, each Undo), with when and why.
- Each trial's **results** (counts only: problems, each arm's accepted and better changes, tokens and yield, the
  winner) are kept as a summary per trial, and per arm as a ``strategy`` TestRun.
- The **frozen control** (pre-registration §7): the method in use two trials back; the first method for the first
  two trials.
- **Stepping stones** (quality-diversity): the best method in each niche (what it tries first, how much it tests,
  how much it reads), so a challenger can start from a different kind of method than the current one.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any

from row_bot.learning.contracts import Release, new_id
from row_bot.learning.improver import strategy as strategy_module
from row_bot.learning.improver.strategy import Strategy

logger = logging.getLogger(__name__)

CHAIN_KEY = "strategy.chain"
TRIALS_KEY = "strategy.trials"
#: How many trial summaries are kept for the view (every TestRun stays in the store).
TRIALS_KEPT = 200


@dataclass(frozen=True)
class Entry:
    """One learning method: its release and its strategy."""

    release: Release
    strategy: Strategy

    @property
    def id(self) -> str:
        return self.release.id

    @property
    def version(self) -> int:
        return self.release.version


def _read(store: Any, release: Release) -> Entry | None:
    raw = store.read_blob(release.content_blob or "") if release.content_blob else None
    if raw is None:
        return None
    try:
        found = strategy_module.from_json(raw.decode("utf-8"))
    except (strategy_module.StrategyError, UnicodeDecodeError):
        logger.warning("Learning couldn't read learning method v%s", release.version)
        return None
    return Entry(release, found.with_version(release.version))


def entries(store: Any) -> list[Entry]:
    """Every learning method kept, oldest first."""

    if not store.exists():
        return []
    found = []
    for item in store.find("release", subkind="strategy", limit=100_000):
        if isinstance(item, Release) and item.kind == "strategy":
            entry = _read(store, item)
            if entry is not None:
                found.append(entry)
    return sorted(found, key=lambda entry: entry.version)


def get(store: Any, release_id: str) -> Entry | None:
    found = store.get("release", str(release_id or "")) if store.exists() else None
    return _read(store, found) if isinstance(found, Release) and found.kind == "strategy" else None


def add(store: Any, method: Strategy, *, parents: Sequence[str] = (), status: str = "candidate",
        attempt_id: str | None = None, at: float | None = None) -> Entry:
    """Keep a new learning method (the next version number) with its parents."""

    version = max((entry.version for entry in entries(store)), default=0) + 1
    method = method.with_version(version)
    blob = store.put_blob(method.to_json().encode("utf-8"))
    release = Release(id=new_id("rel"), kind="strategy", version=version, created_at=float(at if at is not None else store.now()),
                      label=f"Learning method v{version}", parent_ids=tuple(dict.fromkeys(parents))[:64],
                      attempt_id=attempt_id, status=status, content_blob=blob)  # type: ignore[arg-type]
    store.put(release)
    return Entry(release, method)


def set_status(store: Any, entry: Entry, status: str) -> Entry:
    if entry.release.status == status:
        return entry
    release = replace(entry.release, status=status)  # type: ignore[arg-type]
    store.put(release)
    return Entry(release, entry.strategy)


def chain(store: Any) -> list[dict[str, Any]]:
    """The order methods were in use: ``{release, version, at, by ("first" · "trial" · "undo"), trial}``."""

    if not store.exists():
        return []
    try:
        found = json.loads(store.meta_value(CHAIN_KEY, "") or "[]")
    except ValueError:
        return []
    return [item for item in found if isinstance(item, dict) and item.get("release")] if isinstance(found, list) else []


def _set_chain(store: Any, items: Sequence[Mapping[str, Any]]) -> None:
    store.set_meta_values({CHAIN_KEY: json.dumps(list(items), sort_keys=True)})


def ensure_first(store: Any) -> Entry:
    """The first learning method (``strategy.FIRST``: the loop as L3a and L3b built it), kept once."""

    found = entries(store)
    if found:
        return found[0]
    entry = add(store, strategy_module.FIRST, status="in_use")
    _set_chain(store, [{"release": entry.id, "version": entry.version, "at": round(store.now(), 3), "by": "first",
                        "trial": None}])
    return entry


def current(store: Any) -> Entry:
    """The method in use now (the first one until a trial switches)."""

    first = ensure_first(store)
    steps = chain(store)
    if steps:
        found = get(store, str(steps[-1]["release"]))
        if found is not None:
            return found
    return first


def current_or_first(store: Any | None) -> Strategy:
    """The method in use, without creating anything: the first method when Learning has no store yet."""

    if store is None or not store.exists():
        return strategy_module.FIRST
    steps = chain(store)
    if steps:
        found = get(store, str(steps[-1]["release"]))
        if found is not None:
            return found.strategy
    return strategy_module.FIRST


def switch(store: Any, entry: Entry, *, by: str, trial: str | None = None, at: float | None = None) -> Entry:
    """Put ``entry`` in use (a trial's winner, or Undo's way back); the method it replaces is retired."""

    now = round(float(at if at is not None else store.now()), 3)
    before = current(store)
    if before.id == entry.id:
        return entry
    set_status(store, before, "retired")
    entry = set_status(store, entry, "in_use")
    _set_chain(store, [*chain(store), {"release": entry.id, "version": entry.version, "at": now, "by": by,
                                       "trial": trial, "from": before.id}])
    return entry


def undo(store: Any, *, at: float | None = None) -> Entry | None:
    """Undo the last switch a trial made: the method in use before it comes back. None when there's nothing to
    undo (the last change wasn't a trial's)."""

    steps = chain(store)
    if not steps or steps[-1].get("by") != "trial":
        return None
    earlier = get(store, str(steps[-1].get("from") or ""))
    if earlier is None:
        return None
    undone = current(store)
    back = switch(store, earlier, by="undo", trial=steps[-1].get("trial"), at=at)
    set_status(store, undone, "not_used")
    return back


# ── Trial results ───────────────────────────────────────────────────────────


def trials(store: Any) -> list[dict[str, Any]]:
    """Every trial's summary, oldest first (counts only)."""

    if not store.exists():
        return []
    try:
        found = json.loads(store.meta_value(TRIALS_KEY, "") or "[]")
    except ValueError:
        return []
    return [item for item in found if isinstance(item, dict)] if isinstance(found, list) else []


def record_trial(store: Any, summary: Mapping[str, Any]) -> None:
    """Keep a finished trial's summary (and replace one of the same id)."""

    found = [item for item in trials(store) if item.get("id") != summary.get("id")]
    found.append(dict(summary))
    store.set_meta_values({TRIALS_KEY: json.dumps(found[-TRIALS_KEPT:], sort_keys=True)})


def frozen_control(store: Any) -> Entry:
    """The frozen control of the next trial (pre-registration §7): the method in use two trials back (the
    incumbent of the trial before last), and the first method for the first two trials."""

    done = [item for item in trials(store) if not item.get("final")]
    first = ensure_first(store)
    if len(done) < 2:
        return first
    found = get(store, str(done[-2].get("incumbent") or ""))
    return found if found is not None else first


def generations(store: Any) -> list[dict[str, Any]]:
    """The inherited generations judged so far, for the paper's H3 (§7): generation g is the method trial g left
    in use, judged against the frozen control on trial g+1's fresh problems (the last on the final set). Each
    is ``{"trial": the judging trial, "inherited": side, "control": side}`` (a side: its accepted changes as
    (key, kind of work) and its loop's tokens)."""

    found = []
    for item in trials(store):
        judged = item.get("judged")
        if isinstance(judged, dict) and judged.get("inherited") and judged.get("control"):
            found.append({"trial": item["id"], "inherited": judged["inherited"], "control": judged["control"],
                          "final": bool(item.get("final"))})
    return found


def yields(store: Any) -> dict[str, list[float]]:
    """Each method's improvement yields per 1M tokens in the trials it ran in."""

    found: dict[str, list[float]] = {}
    for item in trials(store):
        for arm in item.get("arms") or ():
            if arm.get("release") and arm.get("yield") is not None:
                found.setdefault(str(arm["release"]), []).append(float(arm["yield"]))
    return found


def niche(method: Strategy) -> tuple[str, bool, bool]:
    """A method's niche (quality-diversity): what it tries first, whether it tests more than the first method,
    and whether it reads more examples than the first."""

    first = strategy_module.FIRST
    return (method.methods.prefer[0], method.budget.tests > first.budget.tests,
            method.examples.shown > first.examples.shown)


def stepping_stones(store: Any) -> list[Entry]:
    """The best method of each niche by its mean yield in trials (methods never tried count as 0), the method
    in use always among them."""

    record = yields(store)
    best: dict[tuple[str, bool, bool], tuple[float, Entry]] = {}
    for entry in entries(store):
        found = record.get(entry.id)
        score = sum(found) / len(found) if found else 0.0
        key = niche(entry.strategy)
        if key not in best or score > best[key][0]:
            best[key] = (score, entry)
    stones = [entry for _score, entry in sorted(best.values(), key=lambda item: (-item[0], item[1].version))]
    now = current(store)
    if all(entry.id != now.id for entry in stones):
        stones.insert(0, now)
    return stones


__all__ = [
    "CHAIN_KEY",
    "Entry",
    "TRIALS_KEY",
    "add",
    "chain",
    "current",
    "current_or_first",
    "ensure_first",
    "entries",
    "frozen_control",
    "generations",
    "get",
    "niche",
    "record_trial",
    "set_status",
    "stepping_stones",
    "switch",
    "trials",
    "undo",
    "yields",
]
