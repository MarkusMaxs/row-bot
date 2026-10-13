"""Updater tool — agent-facing wrappers around the updater.

Provides:
- ``row_bot_check_for_updates`` — read-only check
- ``row_bot_install_update``   — prepare an update in the background, or
  restart into a prepared one (approval-gated)

Installs whose host applies updates (``ROW_BOT_UPDATES=external``) get no
update tools at all.
"""

from __future__ import annotations

import logging

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from row_bot.tools.base import BaseTool
from row_bot.tools import registry
from row_bot.tools.approval_gate import gate_action
from row_bot.updates.external import external_mode

logger = logging.getLogger(__name__)


# ── Input schemas ──────────────────────────────────────────────────────

class _NoArgs(BaseModel):
    pass


class _InstallInput(BaseModel):
    version: str = Field(
        default="",
        description=(
            "Optional version string to install (e.g. '5.2.1'). When empty, "
            "the update on offer is installed."
        ),
    )


# ── Implementation ─────────────────────────────────────────────────────

def _check_for_updates() -> str:
    import row_bot.updater as updater

    if external_mode():
        return "This Row-Bot is updated by its host; it doesn't check for updates itself."
    try:
        info = updater.check_for_updates(force=True)
    except Exception as exc:  # pragma: no cover - defensive
        return f"Update check failed: {exc}"
    s = updater.summary_for_status()
    if s.get("dev_install"):
        return "Running from a development checkout — auto-updates are disabled."
    ready = s.get("ready_version")
    if info is None and not ready:
        return f"No update available. You're on v{s['current_version']} ({s['channel']})."
    lines = [f"You're on v{s['current_version']}."]
    if ready:
        lines.append(f"Row-Bot v{ready} is downloaded and ready; it installs when Row-Bot restarts or quits.")
    if info is not None and info.version != ready:
        lines.append(f"Update available: v{info.version} ({info.channel} channel).")
        if info.notes_summary:
            lines.append("")
            lines.append(info.notes_summary)
        if info.html_url:
            lines.append("")
            lines.append(f"Release page: {info.html_url}")
    lines.append("Use row_bot_install_update to install (follows this thread's approval mode).")
    return "\n".join(lines)


def _install_update(version: str = "") -> str:
    """Approval-gated: prepare the update, or restart into the prepared one."""
    import row_bot.updater as updater
    from row_bot.updates.engine import EngineError

    if external_mode():
        return "This Row-Bot is updated by its host; ask the host to update it."
    if updater.is_dev_install():
        return "Running from a development checkout — refusing to install."
    engine = updater.get_engine()
    if engine is None:
        return "The background updater isn't available in this install."

    ready = updater.ready_update()
    if ready and (not version or version == ready):
        blocked = gate_action(
            {
                "tool": "row_bot_install_update",
                "label": f"Restart Row-Bot to install v{ready}",
                "description": (
                    f"Row-Bot v{ready} is downloaded, checked and ready. Row-Bot "
                    "restarts to install it as soon as nothing is running (after "
                    "this reply). Your data stays as it is, and if the new version "
                    "doesn't start, Row-Bot goes back to this one by itself."
                ),
                "args": {"version": ready},
            },
            blocked_message="BLOCKED: Installing updates is disabled in Block approval mode.",
            cancelled_message="Install cancelled.",
        )
        if blocked:
            return blocked
        try:
            # This conversation is itself running work: restart once it ends.
            engine.apply(when="when_idle", expected=ready)
        except EngineError as exc:
            return f"Could not schedule the restart ({exc.code})."
        return f"Row-Bot will restart to install v{ready} as soon as nothing is running."

    info = updater.get_update_state().available
    if version and (info is None or info.version != version):
        info = updater.check_for_updates(force=True)
        if info is None or info.version != version:
            return f"No release matching v{version} is currently available."
    if info is None:
        info = updater.check_for_updates(force=True)
    if info is None:
        return "No update available right now."

    blocked = gate_action(
        {
            "tool": "row_bot_install_update",
            "label": f"Download Row-Bot v{info.version}",
            "description": (
                f"Download Row-Bot v{info.version} from {info.html_url} and prepare "
                "it in the background, next to the running version. Only what "
                "changed is downloaded, and every file is checked. Nothing changes "
                "until Row-Bot restarts or quits."
            ),
            "args": {"version": info.version},
        },
        blocked_message="BLOCKED: Installing updates is disabled in Block approval mode.",
        cancelled_message="Install cancelled.",
    )
    if blocked:
        return blocked
    if not updater.start_background_update(explicit=True):
        status = engine.status()
        if status.phase == "failed" and status.error == "full_installer":
            link = status.full_installer_url or info.html_url
            return f"v{info.version} needs the full installer: {link}"
        if status.phase not in {"downloading", "staging"}:
            return f"Could not start preparing v{info.version}: {status.message or status.phase}."
    return (
        f"Preparing v{info.version} in the background. When it's ready, Settings › "
        "Updates shows Restart now; otherwise it installs the next time Row-Bot quits."
    )


# ── Tool class ─────────────────────────────────────────────────────────

class UpdaterTool(BaseTool):

    @property
    def name(self) -> str:
        return "row_bot_updater"

    @property
    def display_name(self) -> str:
        return "⬆ Auto-update"

    @property
    def description(self) -> str:
        return (
            "Check for Row-Bot updates and install them in the background. "
            "Install actions follow the current thread approval mode."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def destructive_tool_names(self) -> set[str]:
        # row_bot_install_update calls interrupt() internally with its own
        # detailed approval payload — don't double-gate it.
        return set()

    def as_langchain_tools(self) -> list:
        if external_mode():
            # The host applies updates (D11): the agent gets no update tools.
            return []
        return [
            StructuredTool.from_function(
                func=_check_for_updates,
                name="row_bot_check_for_updates",
                description=(
                    "Check GitHub for a newer Row-Bot release on the user's "
                    "channel (stable or beta): the version, its summary, whether "
                    "one is already downloaded and ready, and the release page. "
                    "Read-only — does not download or install anything."
                ),
                args_schema=_NoArgs,
            ),
            StructuredTool.from_function(
                func=_install_update,
                name="row_bot_install_update",
                description=(
                    "Install a Row-Bot update (follows the thread's approval "
                    "mode): prepares it in the background, or, once it is "
                    "ready, restarts Row-Bot when nothing is running."
                ),
                args_schema=_InstallInput,
            ),
        ]

    def execute(self, query: str) -> str:
        return _check_for_updates()


registry.register(UpdaterTool())
