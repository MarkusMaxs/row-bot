"""What restarting now would interrupt (the "Restart now" guard).

Each probe reads only modules the app already loaded, so asking never starts
a subsystem. A probe that fails counts as idle: the guard is a courtesy, the
switch itself is safe at any time.
"""

from __future__ import annotations

import logging
import sys
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

Probe = Callable[[], str | None]


def _loaded(name: str):  # noqa: ANN202
    return sys.modules.get(name)


def agent_runs() -> str | None:
    executions = _loaded("row_bot.runtime.executions")
    if executions is None:
        return None
    count = len(executions.generation_registry.active())
    if not count:
        return None
    return "An agent is working" if count == 1 else f"{count} agents are working"


def workflows() -> str | None:
    tasks = _loaded("row_bot.tasks")
    if tasks is None:
        return None
    running = tasks.get_running_tasks()
    if not running:
        return None
    names = [str(item.get("name") or "") for item in running.values() if isinstance(item, dict)]
    names = [name for name in names if name]
    if len(running) == 1:
        return f"The workflow {names[0]} is running" if names else "A workflow is running"
    return f"{len(running)} workflows are running"


def channel_deliveries() -> str | None:
    orchestrator = _loaded("row_bot.agent_orchestrator")
    if orchestrator is None:
        return None
    if orchestrator.deliveries_in_flight():
        return "Replies are being delivered to your channels"
    return None


def document_jobs() -> str | None:
    jobs = _loaded("row_bot.document_jobs")
    if jobs is None:
        return None
    summary = jobs.get_document_job_service().active_summary()
    if summary.get("active"):
        return "A document is being processed"
    return None


DEFAULT_PROBES: tuple[Probe, ...] = (agent_runs, workflows, channel_deliveries, document_jobs)


def active_work(probes: Iterable[Probe] | None = None) -> list[str]:
    """Short sentences, one per kind of work in progress; empty when idle."""
    found: list[str] = []
    for probe in DEFAULT_PROBES if probes is None else probes:
        try:
            label = probe()
        except Exception:  # noqa: BLE001 - see the module docstring
            logger.debug("Activity probe %s failed", getattr(probe, "__name__", probe), exc_info=True)
            continue
        if label:
            found.append(label[:120])
    return found[:8]


__all__ = ["DEFAULT_PROBES", "Probe", "active_work"]
