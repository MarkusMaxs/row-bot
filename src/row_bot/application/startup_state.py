"""Where start-up stands.

Written by the start-up sequence in ``row_bot.app``; read by ``/readyz``,
``/api/startup-state`` (the launcher's splash) and ``scripts/smoke_app.py``.
The warnings also reach the React client and Monitor through ``app_notices``.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

ready = False
status = "Starting…"
warnings: list[str] = []

_restart_lock = threading.Lock()
_app_initiated: bool | None = None


def last_exit_was_app_initiated() -> bool:
    """Whether Row-Bot restarted itself into this run (installing an update, Repair).

    Goals that were working when Row-Bot closed come back paused after a normal
    launch; only a restart Row-Bot caused lets them carry on by themselves
    (decision D4, ``goals.settle_interrupted_goals``). The update path records
    the reason in ``state.json`` when it switches and relaunches
    (``updates.switcher``); the first call consumes it and this run keeps the
    answer, so the next launch is a normal one again. A quit, a crash, a
    rollback by the launcher, or a source checkout is a normal launch.
    """
    global _app_initiated
    with _restart_lock:
        if _app_initiated is None:
            try:
                from row_bot.updates.switcher import consume_restart_reason

                reason = consume_restart_reason()
            except Exception:  # noqa: BLE001 - never block start-up on it
                logger.debug("Could not read the restart reason", exc_info=True)
                reason = None
            if reason:
                logger.info("Row-Bot restarted itself (%s)", reason)
            _app_initiated = reason is not None
        return _app_initiated
