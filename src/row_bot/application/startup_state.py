"""Where start-up stands.

Written by the start-up sequence in ``row_bot.app``; read by ``/readyz``,
``/api/startup-state`` (the launcher's splash) and ``scripts/smoke_app.py``.
The warnings also reach the React client and Monitor through ``app_notices``.
"""
from __future__ import annotations

ready = False
status = "Starting…"
warnings: list[str] = []


def last_exit_was_app_initiated() -> bool:
    """Whether Row-Bot itself ended its previous run (applying an update, crash recovery).

    Goals that were working when Row-Bot closed come back paused after a normal
    launch; only a restart Row-Bot caused lets them carry on by themselves
    (decision D4, ``goals.settle_interrupted_goals``). Telling the two apart needs
    the restart-reason marker from the install/update work (5.2 Phase 3). Until it
    is wired here, every launch counts as a normal one.
    """
    return False
