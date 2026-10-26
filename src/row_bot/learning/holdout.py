"""Held-out corrections (O87): every 2nd confirmed correction becomes a hidden problem.

The audit vault holds only decisions nobody corrected (O19's eligibility), so "lessons vs none"
on hidden real work would be tested only where the main model was already right. From the first
build that drafts or trains on corrections, **every 2nd confirmed correction from exact capture**,
in time order, is held out: its decision gets the ``held_out`` mark that datasets, drafting and
practice already leave out (:meth:`LearningStore.held_out_ids`), so nothing ever trains on it, and
the ``held_out_correction`` mark that says the vault should hold it with its corrected action as
the reference (L3a stores and tests it; :func:`waiting_for_vault` is that seam).

- **Confirmed correction**: a Teach or a next-message correction (``corrections`` confirms those
  first, on every pass) linked to the decision it corrects, not withdrawn.
- **Exact capture** (O89): the decision's fidelity is ``exact`` or ``redacted``. Imported past
  chats (``partial``) are never held out, never hidden, and don't count towards "every 2nd".
- **Only with a reference (amendment A3, 23 Oct)**: a confirmed correction counts toward "every 2nd"
  only once it has a reference, the person's accepted redo (``corrections.accepted_redo``) or their
  answer to Teach's "What should it have done?", and it is counted in time order at the moment that
  reference exists. A correction with no reference is not held out (it would be neither trainable nor
  testable); one still without a reference after :data:`REFERENCE_DAYS` never counts.
- **The main model's only (amendment A5)**: a correction of a reply the personal model gave, or whose
  redo the personal model gave, is never held out (the vault's references are the main model's).
- **Time order, append-only**: the decisions counted are kept in ``learning.db`` in the order they
  were counted, so a correction once held stays held (those held under the earlier rule too), a
  decision corrected again later is never counted twice (its redo may have trained meanwhile), and
  one trained on is never hidden later. When this build first applied A3 is recorded beside them.
  These rules are fixed points (E4), like the vault's every 4th decision.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from row_bot.learning.contracts import Decision, Episode, Signal
from row_bot.learning.store import MARK_HELD_OUT, MARK_HELD_OUT_CORRECTION, LearningStore

logger = logging.getLogger(__name__)

HOLD_EVERY = 2
CORRECTION_KINDS = frozenset({"teach", "next_message_correction"})
EXACT = frozenset({"exact", "redacted"})
#: A3: a correction still without a reference this long after it was made never counts.
REFERENCE_DAYS = 7
_HORIZON = "held_out_corrections.horizon"  # the earlier rule's: the newest correction it considered
_COUNTED = "held_out_corrections.counted"  # the decisions counted so far (ids), in order
RULES_KEY = "held_out_corrections.rules"  # when this build first applied each amendment (A3, A5)
PERSONAL = "row-bot-personal"


def personal(decision: Any) -> bool:
    """Whether the personal model made this decision (A5: never a reference, never a hidden problem)."""

    return PERSONAL in str(getattr(decision, "model_ref", "") or "").lower()


def record_rules(store: LearningStore, rules: Iterable[str], key: str = RULES_KEY) -> dict[str, Any]:
    """Record, once, when this build first applied each amendment (its date and the app's version)."""

    try:
        found = json.loads(store.meta_value(key, "{}") or "{}")
    except ValueError:
        found = {}
    found = found if isinstance(found, dict) else {}
    missing = [rule for rule in rules if rule not in found]
    if missing:
        from row_bot.version import __version__

        for rule in missing:
            found[rule] = {"since": round(store.now(), 3), "app_version": __version__}
        store.set_meta_values({key: json.dumps(found, sort_keys=True)})
    return found


@dataclass(frozen=True)
class HoldReport:
    """What one pass did (counts only)."""

    considered: int
    counted: int
    held: tuple[str, ...]  # the corrected decisions newly held out


def _confirmed(signals: Iterable[object], horizon: float) -> list[Signal]:
    found = [item for item in signals if isinstance(item, Signal) and item.kind in CORRECTION_KINDS
             and item.decision_id and item.withdrawn_at is None and item.created_at > horizon]
    return sorted(found, key=lambda item: (item.created_at, item.id))


def _reference_at(store: LearningStore, book: Any, signal: Signal, decision: Decision) -> float | None:
    """When the correction's reference came to exist (A3), or None while it has none: a Teach's own
    answer on a reply (when it was given), else the redo the person accepted (when it started), the main
    model's (A5)."""

    from row_bot.learning import corrections

    if signal.kind == "teach" and str(getattr(signal, "answer", "") or "").strip() and decision.kind == "model_call":
        return float(signal.created_at)
    redo = corrections.accepted_redo(store, decision, book=book)
    first = corrections.first_call(book, redo) if redo is not None else None
    if first is None or personal(first):
        return None
    return float(redo.started_at)


def mark_corrections(store: LearningStore) -> HoldReport:
    """Hold out every 2nd confirmed correction from exact capture that has a reference, counted in the
    time order its reference came to exist (A3)."""

    if not store.exists():
        return HoldReport(0, 0, ())
    from row_bot.learning import corrections

    corrections.confirm(store)  # the person's next-message corrections count too (O12)
    record_rules(store, ("A3", "A5"))
    try:
        decided: list[str] = [str(item) for item in json.loads(store.meta_value(_COUNTED, "[]"))]
    except ValueError:
        decided = []
    known = set(decided)
    oldest = store.now() - REFERENCE_DAYS * 86_400
    pending = [item for item in _confirmed(store.find("signal", limit=100_000), 0.0)
               if str(item.decision_id) not in known and item.created_at >= oldest]
    if not pending:
        return HoldReport(0, 0, ())
    book = corrections.Book(store)
    ready: list[tuple[float, float, str, Decision]] = []
    for signal in pending:
        decision = book.decisions.get(str(signal.decision_id)) or store.get("decision", str(signal.decision_id))
        if not isinstance(decision, Decision) or decision.id in known:
            continue  # gone (purged), or corrected again: one problem, counted once
        if decision.fidelity not in EXACT or personal(decision):
            continue  # imported (partial), or the personal model's own reply (A5): never hidden, not counted
        episode = book.episodes.get(decision.episode_id) or store.get("episode", decision.episode_id)
        if isinstance(episode, Episode) and episode.historical:
            continue
        at = _reference_at(store, book, signal, decision)
        if at is None:
            continue  # no reference yet: not held out, looked at again on the next pass (A3)
        known.add(decision.id)
        ready.append((at, signal.created_at, signal.id, decision))
    held: list[Decision] = []
    for _at, _made, _signal, decision in sorted(ready, key=lambda item: item[:3]):
        decided.append(decision.id)
        if len(decided) % HOLD_EVERY == 0:
            held.append(decision)
    items = [("decision", item.id, item.conversation_id) for item in held]
    if items:
        store.add_marks(items, MARK_HELD_OUT)
        store.add_marks(items, MARK_HELD_OUT_CORRECTION)
    store.set_meta_values({_COUNTED: json.dumps(decided)})
    if held:
        logger.info("Learning held out %d corrected decision(s) as hidden problems", len(held))
    return HoldReport(len(pending), len(ready), tuple(item.id for item in held))


def waiting_for_vault(store: LearningStore) -> set[str]:
    """Corrected decisions held out that the vault should hold: ``evaluation.refresh_audit`` stores each
    with its corrected action as the reference once the person accepted a redo; they stay out of
    everything else either way."""

    return store.held_out_correction_ids()


__all__ = ["CORRECTION_KINDS", "HOLD_EVERY", "HoldReport", "REFERENCE_DAYS", "mark_corrections", "personal",
           "record_rules", "waiting_for_vault"]
