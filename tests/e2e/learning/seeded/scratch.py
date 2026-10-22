"""Where the release check may write, and when it may use the graphics card (S1).

- :func:`check_scratch` refuses a folder that is (or is inside) the everyday Row-Bot profile, its
  backups, the eval profile or a Row-Bot checkout: the seeded runs only ever write under the scratch
  folders they are given.
- :class:`GpuGate` books the shared graphics card in a booking file before GPU-heavy work (a line
  ``<time> <lane> START <what>`` and later ``<time> <lane> END <what>``), waits while another lane has an
  open booking, and waits while the person's own Row-Bot trains (its training engine runs from the
  everyday profile's ``runtimes`` folder): **the person's Row-Bot always wins**.

Neither imports Row-Bot.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path

#: How long the gate waits between looks while the card is taken.
WAIT_SECONDS = 60.0
#: A checkout's own ignored scratch folders, where test runs keep their temporary folders.
SCRATCH_IN_CHECKOUT = (".tmp", ".testtmp")
_LINE = re.compile(r"^\s*(\d{1,2}:\d{2})\s+(\S+)\s+(START|END)\b\s*(.*)$")


class ScratchError(RuntimeError):
    """A folder the release check must not use, or a run it can't start."""


def everyday_profile() -> Path:
    return (Path.home() / ".row-bot").resolve()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


#: More folders a seeded run must never write in (the everyday profile's backups, say), separated by
#: ``os.pathsep``.
PROTECT_ENV = "ROW_BOT_SEEDED_PROTECT"


def forbidden_roots(eval_profile: Path | None = None, extra: Iterable[Path] = ()) -> list[Path]:
    """Folders a seeded run never writes in."""

    roots = [everyday_profile()]
    roots.extend(Path(item) for item in os.environ.get(PROTECT_ENV, "").split(os.pathsep) if item.strip())
    if eval_profile is not None:
        roots.append(Path(eval_profile))
    roots.extend(extra)
    return roots


def check_scratch(folder: Path, eval_profile: Path | None = None, *, extra: Iterable[Path] = ()) -> Path:
    """``folder``, resolved, when a seeded run may write there; otherwise :class:`ScratchError`."""

    resolved = Path(folder).resolve()
    for root in forbidden_roots(eval_profile, extra):
        if _inside(resolved, root) or _inside(root, resolved):
            raise ScratchError(f"{resolved} is or holds {root}: the release check writes only to scratch folders")
    probe = resolved
    while probe != probe.parent:
        if (probe / ".git").exists() and (probe / "src" / "row_bot").is_dir():
            if resolved != probe and resolved.relative_to(probe).parts[0] in SCRATCH_IN_CHECKOUT:
                break  # the checkout's own ignored scratch folders (the test runs' temporary folders)
            raise ScratchError(f"{resolved} is inside a Row-Bot checkout ({probe}): use a scratch folder")
        probe = probe.parent
    return resolved


def open_bookings(text: str) -> dict[str, str]:
    """Each lane's open booking (a START with no END after it): lane -> what."""

    found: dict[str, str] = {}
    for line in text.splitlines():
        match = _LINE.match(line)
        if not match:
            continue
        _, lane, word, what = match.groups()
        if word == "START":
            found[lane] = what.strip()
        else:
            found.pop(lane, None)
    return found


def person_is_training(processes: Callable[[], Iterable[str]] | None = None,
                       profile: Path | None = None) -> bool:
    """Whether the person's own Row-Bot runs its training engine now (a process started from the
    everyday profile's ``runtimes`` folder)."""

    folder = str((profile or everyday_profile()) / "runtimes").lower()
    for path in (processes or _process_paths)():
        if path and path.lower().startswith(folder):
            return True
    return False


def _process_paths() -> Iterable[str]:
    try:
        import psutil
    except ImportError:
        return ()
    found = []
    for process in psutil.process_iter(["exe"]):
        try:
            found.append(str(process.info.get("exe") or ""))
        except Exception:  # noqa: BLE001 - a process that ended or that we may not read
            continue
    return found


class GpuGate:
    """Books the shared card for this lane (see the module's docstring). Without a booking file it only
    waits for the person's own training."""

    def __init__(self, booking: Path | None, lane: str, *, label: str = "S1",
                 clock: Callable[[], str] = lambda: time.strftime("%H:%M"),
                 sleep: Callable[[float], None] = time.sleep,
                 training: Callable[[], bool] = person_is_training,
                 say: Callable[[str], None] = lambda text: print(text, flush=True)) -> None:
        self.booking = booking
        self.lane = lane
        self.label = label
        self.clock = clock
        self.sleep = sleep
        self.training = training
        self.say = say
        self.held: str | None = None
        self.waited = 0.0

    def _read(self) -> str:
        try:
            return self.booking.read_text(encoding="utf-8") if self.booking else ""
        except OSError:
            return ""

    def _append(self, line: str) -> None:
        if self.booking is None:
            return
        with self.booking.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line.rstrip("\n") + "\n")

    def acquire(self, what: str) -> None:
        """Wait until the card is ours, then book it (once per holding: a second call is a no-op)."""

        if self.held is not None:
            return
        while True:
            others = {lane: item for lane, item in open_bookings(self._read()).items() if lane != self.lane}
            if self.training():
                self.say(f"GPU: the person's own Row-Bot is training; waiting ({self.label})")
            elif others:
                self.say(f"GPU: {', '.join(sorted(others))} booked it ({'; '.join(others.values())[:120]}); waiting")
            else:
                break
            self.sleep(WAIT_SECONDS)
            self.waited += WAIT_SECONDS
        mine = open_bookings(self._read()).get(self.lane)
        if mine is None:
            self._append(f"{self.clock()} {self.lane} START {self.label}: {what} (yields to the person's Learning engine)")
        self.held = what

    def release(self) -> None:
        if self.held is None and self.lane not in open_bookings(self._read()):
            return
        if self.lane in open_bookings(self._read()):
            self._append(f"{self.clock()} {self.lane} END {self.label}")
        self.held = None


__all__ = ["GpuGate", "PROTECT_ENV", "ScratchError", "check_scratch", "everyday_profile", "forbidden_roots", "open_bookings",
           "person_is_training"]
