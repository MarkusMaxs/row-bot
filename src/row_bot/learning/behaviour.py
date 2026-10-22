"""Ways of working (D18, E7): a versioned overlay of whitelisted guidance slots, read at turn start.

A **way of working** is a short standing instruction Learning found in the person's own work, tested
before use (``changes``) and put into use by the person (or automatically, D24). It lives in one of
the **whitelisted slots** below; nothing else about Row-Bot is reachable from here (approvals, tools,
permissions, spending and who can reach Row-Bot stay outside Learning, E4).

Slots wired (L3b chose them; ``evolution.BEHAVIOUR_SLOTS`` is the same list):

- ``guidance``: how to approach the work ("check which document is current before answering");
- ``tool_guidance``: how to use the tools ("read the calendar before booking");
- ``delegation``: when to hand work to a helper (``delegate_work``).

Recall settings and step budgets were drafted as slots in L3a and are **not wired**: a knob's value
can't come safely from a diagnosis's words, a step budget is spending (D24 keeps it outside Learning),
and a recall setting would change how lessons themselves are recalled (H2 compares lessons on and off).

**The overlay** is one file beside Learning's settings, outside ``learning.db``, so ways of working in
use stay in use while Learning is off or paused and after "Delete learning data" (UX §8). Each use or
undo writes a new version; **Undo** of the newest change makes the version before it current again.
The agent reads the current version's text **once per turn** (``agent``, cached by turn) as the last
stable system section: a new version costs one prompt-cache miss on its first turn, and nothing
changes mid-turn (A1's prefix contract).

The file holds the ways of working's own words and their change ids; no request, reply or chat.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FILE = "learning_ways_of_working.json"
FORMAT = "row_bot.learning.ways_of_working"
SCHEMA_VERSION = 1
SLOTS: tuple[str, ...] = ("guidance", "tool_guidance", "delegation")
HEADINGS = {
    "guidance": "How to work",
    "tool_guidance": "Using tools",
    "delegation": "Handing work to a helper",
}
#: Plain words for each slot (the review sheet's "What changes").
SLOT_WORDS = {
    "guidance": "How Row-Bot approaches the work",
    "tool_guidance": "How Row-Bot uses its tools",
    "delegation": "When Row-Bot hands work to a helper",
}
MAX_PER_SLOT = 6
MAX_TEXT = 400
MAX_SECTION = 2_400
MAX_VERSIONS = 200
SECTION_TITLE = "WAYS OF WORKING"
SECTION_LEAD = (
    "Row-Bot learned these from the person's own work and tested each one before using it. Follow them "
    "unless the person asks for something else. They never change what needs the person's approval."
)
_LOCK = threading.RLock()


class BehaviourError(ValueError):
    """A refused use or undo; ``code`` is stable (``unknown_slot``, ``empty``, ``too_long``, ``slot_full``,
    ``not_in_use``)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class Entry:
    """One way of working in use: the change it came from, its slot and its words."""

    change_id: str
    slot: str
    text: str
    title: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"change_id": self.change_id, "slot": self.slot, "text": self.text, "title": self.title}


@dataclass(frozen=True)
class Version:
    """One version of the overlay: what was in use from ``created_at``, and why it was made."""

    number: int
    entries: tuple[Entry, ...]
    created_at: float
    # use: a change went into use; undo: one was taken out.
    reason: str = "use"
    change_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"number": self.number, "entries": [entry.to_dict() for entry in self.entries],
                "created_at": round(self.created_at, 3), "reason": self.reason, "change_id": self.change_id}


@dataclass(frozen=True)
class Overlay:
    versions: tuple[Version, ...] = ()
    # The version in force (0: none, Row-Bot's own ways).
    active: int = 0
    history: tuple[Mapping[str, Any], ...] = field(default_factory=tuple)

    def current(self) -> Version | None:
        return next((item for item in self.versions if item.number == self.active), None)

    @property
    def entries(self) -> tuple[Entry, ...]:
        found = self.current()
        return found.entries if found is not None else ()


def path(root: Path | None = None) -> Path:
    from row_bot import learning as gate

    return (root if root is not None else gate.data_dir()) / FILE


def _entry(raw: Any) -> Entry | None:
    if not isinstance(raw, Mapping):
        return None
    slot = str(raw.get("slot") or "")
    text = str(raw.get("text") or "").strip()
    change = str(raw.get("change_id") or "")
    if slot not in SLOTS or not text or not change:
        return None
    # ``use`` keeps each one to MAX_TEXT; the section as a whole is capped when it is rendered.
    return Entry(change[:128], slot, text[:MAX_SECTION], str(raw.get("title") or "")[:200])


def read(root: Path | None = None) -> Overlay:
    """The overlay (empty when there is none, or when the file can't be read: Row-Bot's own ways)."""

    try:
        raw = json.loads(path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return Overlay()
    if not isinstance(raw, Mapping) or raw.get("format") != FORMAT:
        return Overlay()
    versions = []
    for item in raw.get("versions") or ():
        if not isinstance(item, Mapping):
            continue
        try:
            number = int(item.get("number") or 0)
        except (TypeError, ValueError):
            continue
        if number < 1:
            continue
        entries = tuple(entry for entry in map(_entry, item.get("entries") or ()) if entry is not None)
        versions.append(Version(number, entries, float(item.get("created_at") or 0.0), str(item.get("reason") or "use"),
                                str(item.get("change_id") or "")))
    try:
        active = int(raw.get("active") or 0)
    except (TypeError, ValueError):
        active = 0
    if active and not any(item.number == active for item in versions):
        active = 0
    history = tuple(dict(item) for item in raw.get("history") or () if isinstance(item, Mapping))
    return Overlay(tuple(versions), active, history[-MAX_VERSIONS:])


def _write(overlay: Overlay, root: Path | None = None) -> Overlay:
    from row_bot.learning import atomic

    kept = overlay.versions[-MAX_VERSIONS:]
    if overlay.active and not any(item.number == overlay.active for item in kept):
        kept = (*kept, *(item for item in overlay.versions if item.number == overlay.active))
    body = {"format": FORMAT, "schema_version": SCHEMA_VERSION, "active": overlay.active,
            "versions": [item.to_dict() for item in kept], "history": list(overlay.history[-MAX_VERSIONS:])}
    target = path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    atomic.write_text(target, json.dumps(body, indent=1, sort_keys=True))
    return replace(overlay, versions=kept)


def render(entries: Sequence[Entry]) -> str:
    """The stable system section for these entries ("" for none), at most :data:`MAX_SECTION` characters."""

    if not entries:
        return ""
    lines = [f"{SECTION_TITLE}:", SECTION_LEAD]
    for slot in SLOTS:
        texts = [entry.text for entry in entries if entry.slot == slot]
        if not texts:
            continue
        lines.append(f"{HEADINGS[slot]}:")
        lines.extend(f"- {' '.join(text.split())}" for text in texts)
    text = "\n".join(lines)
    return text if len(text) <= MAX_SECTION else text[: MAX_SECTION - 1].rstrip() + "…"


def section_text(root: Path | None = None) -> str:
    """The current version's section, as the agent puts it in a turn's system prompt."""

    return render(read(root).entries)


def check(slot: str, text: str) -> str:
    """The text a slot would hold, or raise :class:`BehaviourError`. E9's words check is the caller's
    (``changes``), at use and after an edit."""

    if slot not in SLOTS:
        raise BehaviourError("unknown_slot")
    clean = " ".join(str(text or "").split())
    if not clean:
        raise BehaviourError("empty")
    if len(clean) > MAX_TEXT:
        raise BehaviourError("too_long")
    return clean


def in_use(change_id: str, root: Path | None = None) -> bool:
    return any(entry.change_id == change_id for entry in read(root).entries)


def use(change_id: str, slot: str, text: str, *, title: str = "", root: Path | None = None,
        clock: Callable[[], float] = time.time) -> Version:
    """Put a way of working into use: a new version with it added (its slot keeps at most
    :data:`MAX_PER_SLOT`). Using one already in use changes nothing and returns the current version."""

    clean = check(slot, text)
    with _LOCK:
        overlay = read(root)
        current = overlay.current()
        if current is not None and any(entry.change_id == change_id for entry in current.entries):
            return current
        entries = list(overlay.entries)
        if sum(1 for entry in entries if entry.slot == slot) >= MAX_PER_SLOT:
            raise BehaviourError("slot_full")
        entries.append(Entry(str(change_id)[:128], slot, clean, str(title or "")[:200]))
        if len(render(entries)) >= MAX_SECTION:
            raise BehaviourError("too_long")
        number = max((item.number for item in overlay.versions), default=0) + 1
        version = Version(number, tuple(entries), float(clock()), "use", str(change_id)[:128])
        event = {"at": round(float(clock()), 3), "event": "use", "change_id": str(change_id)[:128], "version": number}
        _write(Overlay((*overlay.versions, version), number, (*overlay.history, event)), root)
        logger.info("A way of working went into use (version %s)", number)
        return version


def undo(change_id: str, *, root: Path | None = None, clock: Callable[[], float] = time.time) -> Version | None:
    """Take a way of working out of use. When it was the newest change, the version before it becomes
    current again (E7: Undo restores the previous version); otherwise a new version leaves it out.
    Returns the version now current (None: Row-Bot's own ways)."""

    with _LOCK:
        overlay = read(root)
        current = overlay.current()
        if current is None or not any(entry.change_id == change_id for entry in current.entries):
            raise BehaviourError("not_in_use")
        remaining = tuple(entry for entry in current.entries if entry.change_id != change_id)
        previous = next((item for item in reversed(overlay.versions)
                         if item.number < current.number and item.entries == remaining), None)
        if current.change_id == change_id and current.reason == "use" and (previous is not None or not remaining):
            active = previous.number if previous is not None else 0
            versions = overlay.versions
            result = previous
        else:
            number = max(item.number for item in overlay.versions) + 1
            result = Version(number, remaining, float(clock()), "undo", str(change_id)[:128])
            versions = (*overlay.versions, result)
            active = number
        event = {"at": round(float(clock()), 3), "event": "undo", "change_id": str(change_id)[:128], "version": active}
        _write(Overlay(versions, active, (*overlay.history, event)), root)
        logger.info("A way of working was taken out of use (version %s now)", active or "none")
        return result


def describe(slot: str) -> str:
    return SLOT_WORDS.get(slot, "How Row-Bot works")


__all__ = [
    "BehaviourError",
    "Entry",
    "FILE",
    "HEADINGS",
    "MAX_PER_SLOT",
    "MAX_SECTION",
    "MAX_TEXT",
    "Overlay",
    "SLOTS",
    "SLOT_WORDS",
    "Version",
    "check",
    "describe",
    "in_use",
    "path",
    "read",
    "render",
    "section_text",
    "undo",
    "use",
]
