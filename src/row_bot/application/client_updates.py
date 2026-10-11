"""Update status and commands for the client (Settings › Updates).

Reads never check, download or install anything. Commands are bound to the
revision the person saw and are idempotent by command id. The engine
(:mod:`row_bot.updates`) does the work in the background; a restart into a
ready version is the only command that stops Row-Bot, and it is refused while
work is running unless the person confirms it (or chooses "when idle" or "on
quit"). With ``ROW_BOT_UPDATES=external`` commands become requests to the
host (:mod:`row_bot.updates.external`).
"""

from __future__ import annotations

import hashlib
import json
from threading import RLock
from typing import Any
from uuid import UUID

from row_bot.application.client_platform import ClientPlatformError

_LOCK = RLock()
_RECEIPTS: dict[str, dict[str, Any]] = {}

_ACTIONS = frozenset(
    {
        "check", "skip", "clear_skipped", "download", "cancel", "retry", "apply",
        "rollback", "set_auto_check", "set_auto_download", "remove_legacy", "pause", "resume",
    }
)
_EXTERNAL_ACTIONS = {"check": "check", "apply": "update_now", "skip": "skip", "pause": "pause", "resume": "resume"}
_ENGINE_ERRORS = {
    "update_not_ready", "update_restart_busy", "update_busy", "update_no_previous",
    "update_changed", "update_unavailable",
}


def _owner():
    # The updater is imported only for an explicit updates view or action.
    from row_bot import updater

    return updater


def _layout():
    from row_bot import install_layout

    return install_layout.detect()


def _engine_view(updater: Any, mode: str) -> dict[str, Any]:
    from row_bot.updates.engine import EngineStatus

    engine = updater.get_engine() if mode == "self" else None
    status = engine.status() if engine is not None else EngineStatus()
    phase = status.phase
    if updater.is_checking() and phase in {"idle", "verified", "failed"}:
        phase = "checking"
    url = status.full_installer_url or ""
    return {
        "phase": phase,
        "version": (status.version or None) and status.version[:64],
        "step": (status.step or None) and status.step[:64],
        "message": (status.message or None) and status.message[:512],
        "error": (status.error or None) and status.error[:64],
        "retryable": bool(status.retryable),
        "apply_when": status.apply_when,
        "full_installer_url": url[:2048] if url.startswith("https://") else None,
        "downloaded": min(1_000_000_000_000, max(0, int(status.downloaded))),
        "total": min(1_000_000_000_000, max(0, int(status.total))),
    }


def _previous_version(mode: str) -> str | None:
    if mode != "self":
        return None
    from row_bot import install_layout
    from row_bot.updates.switcher import runnable_version

    layout = _layout()
    if layout.root is None:
        return None
    state = install_layout.load_state(layout.root, layout.kind)
    if state is None or not state.previous or state.previous == state.active:
        return None
    return state.previous[:64] if runnable_version(layout, state.previous) else None


def _legacy_install(mode: str) -> bool:
    if mode != "self":
        return False
    try:
        from row_bot.updates import legacy

        return legacy.pending(_layout())
    except Exception:
        return False


def _host_view(mode: str) -> dict[str, Any] | None:
    if mode != "external":
        return None
    from row_bot.updates import external

    status = external.read_status()
    request = external.read_request()
    waiting = None
    if request and request.get("action") and request.get("id") != status.get("handled_request"):
        waiting = {"action": request["action"], "requested_at": request.get("requested_at")}
    return {
        "state": status["state"],
        "current_version": status["current_version"],
        "available_version": status["available_version"],
        "message": status["message"],
        "notes_url": status["notes_url"],
        "updated_at": status["updated_at"],
        "request": waiting,
    }


def read_updates() -> dict[str, Any]:
    """Read cached release state; never check, download, or install on render."""
    updater = _owner()
    with _LOCK:
        state = updater.get_update_state()
        release = state.available
        skipped = updater.skipped_release()
        mode = updater.update_mode()
        engine = _engine_view(updater, mode)
        values = {
            "channel": state.channel if state.channel in {"stable", "beta"} else "stable",
            "current_version": state.current_version[:64],
            "last_check": state.last_check[:64] if state.last_check else None,
            "last_success": state.last_success[:64] if state.last_success else None,
            # Only the skip holding back the release on offer (B261).
            "skipped_versions": [skipped[:64]] if skipped else [],
            "available": (
                {
                    "version": release.version[:64],
                    "channel": release.channel if release.channel in {"stable", "beta"} else "stable",
                    "published_at": release.published_at[:64],
                    "notes": release.notes_md[:16000],
                    "html_url": release.html_url[:2048],
                    "asset_size": min(1_000_000_000_000, max(0, release.asset_size)),
                    "verified_manifest": bool(release.sha256 or release.update_manifest_sha256),
                }
                if release
                else None
            ),
            "dev_install": mode == "unavailable",
            "mode": mode,
            "auto_check": bool(state.auto_check),
            "auto_download": bool(state.auto_download),
            "engine": engine,
            "previous_version": _previous_version(mode),
            "legacy_install": _legacy_install(mode),
            "host": _host_view(mode),
        }
        # Progress and running work change by the second; a command is bound
        # to everything else the person saw.
        bound = {**values, "engine": {k: v for k, v in engine.items() if k not in {"downloaded", "total"}}}
        revision = hashlib.sha256(
            json.dumps(bound, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        from row_bot.updates.activity import active_work

        work = active_work() if engine["phase"] == "ready" else []
        return {"schema_version": 1, "revision": revision, **values, "active_work": work}


def _engine(updater: Any) -> Any:
    engine = updater.get_engine()
    if engine is None:
        raise ClientPlatformError("update_unavailable")
    return engine


def _run_external(action: str, version: str) -> str:
    from row_bot.updates import external

    host_action = _EXTERNAL_ACTIONS.get(action)
    if host_action is None:
        raise ClientPlatformError("update_unavailable")
    external.write_request(host_action, version=version or None)
    return "requested"


def _run_self(
    updater: Any, before: dict[str, Any], *, action: str, version: str,
    when: str | None, force: bool, enabled: bool | None,
) -> str:
    from row_bot.updates.engine import EngineError

    if action in {"pause", "resume"}:
        raise ClientPlatformError("update_unavailable")
    if action in {"set_auto_check", "set_auto_download"}:
        if enabled is None:
            raise ClientPlatformError("invalid_update_command")
        updater.set_preference(action.removeprefix("set_"), enabled)
        if action == "set_auto_download" and enabled:
            updater.start_background_update()
        return "completed"
    if before["dev_install"]:
        raise ClientPlatformError("update_unavailable")
    try:
        if action == "check":
            updater.check_for_updates(force=True)
            after = updater.get_update_state()
            updater.start_background_update()
            return "completed" if after.last_success != before["last_success"] else "failed"
        if action == "skip":
            available = before["available"]
            if not available or not version or version != available["version"]:
                raise ClientPlatformError("update_changed")
            updater.skip_version(version)
            return "completed"
        if action == "clear_skipped":
            # "Show it again": the check offers the release once more.
            updater.clear_skipped_versions()
            updater.check_for_updates(force=True)
            return "completed" if updater.get_update_state().last_success != before["last_success"] else "failed"
        engine = _engine(updater)
        if action in {"download", "retry"}:
            available = before["available"]
            if not available or (version and version != available["version"]):
                raise ClientPlatformError("update_changed")
            started = updater.start_background_update(explicit=True)
            return "completed" if started or engine.status().phase in {"downloading", "staging", "ready"} else "failed"
        if action == "cancel":
            return "completed" if engine.cancel() else "failed"
        if action == "apply":
            if when not in {"now", "when_idle", "on_quit"}:
                raise ClientPlatformError("invalid_update_command")
            engine.apply(when=when, force=force, expected=version or None)
            return "completed"
        if action == "rollback":
            if not before["previous_version"] or (version and version != before["previous_version"]):
                raise ClientPlatformError("update_changed")
            engine.rollback()
            return "completed"
        if action == "remove_legacy":
            from row_bot.updates import legacy

            if not before["legacy_install"]:
                raise ClientPlatformError("update_changed")
            return "completed" if legacy.start_removal(_layout()) else "failed"
    except EngineError as exc:
        raise ClientPlatformError(exc.code if exc.code in _ENGINE_ERRORS else "update_unavailable") from None
    raise ClientPlatformError("invalid_update_command")


def execute_update_choice(
    *, owner_id: str, command_id: str, expected_revision: str, action: str, version: str = "",
    when: str | None = None, force: bool = False, enabled: bool | None = None,
) -> dict[str, Any]:
    """Run one update command with an idempotent command identity."""
    try:
        if str(UUID(command_id)) != command_id:
            raise ValueError
    except ValueError:
        raise ClientPlatformError("invalid_update_command") from None
    if action not in _ACTIONS:
        raise ClientPlatformError("invalid_update_command")
    if version and action in {"check", "clear_skipped", "cancel", "set_auto_check", "set_auto_download",
                              "remove_legacy", "pause", "resume"}:
        raise ClientPlatformError("invalid_update_command")
    identity = {"action": action, "version": version, "when": when, "force": force, "enabled": enabled}
    with _LOCK:
        receipt_key = f"{owner_id}:{command_id}"
        prior = _RECEIPTS.get(receipt_key)
        if prior:
            if prior["identity"] != identity:
                raise ClientPlatformError("update_command_conflict")
            return prior["receipt"]
        before = read_updates()
        if before["revision"] != expected_revision:
            raise ClientPlatformError("update_changed")
        updater = _owner()
        if before["mode"] == "external" and action not in {"set_auto_check", "set_auto_download"}:
            status = _run_external(action, version)
        else:
            status = _run_self(updater, before, action=action, version=version, when=when, force=force, enabled=enabled)
        receipt = {
            "schema_version": 1,
            "command_id": command_id,
            "status": status,
            "snapshot": read_updates(),
        }
        if len(_RECEIPTS) >= 64:
            _RECEIPTS.pop(next(iter(_RECEIPTS)))
        _RECEIPTS[receipt_key] = {"identity": identity, "receipt": receipt}
        return receipt


def apply_for_launcher() -> dict[str, Any]:
    """The tray's "Restart to Update": restart now, or once running work ends."""
    from row_bot.updates.engine import EngineError

    engine = _owner().get_engine()
    if engine is None:
        return {"status": "unavailable"}
    try:
        engine.apply(when="now")
        return {"status": "restarting"}
    except EngineError as exc:
        if exc.code != "update_restart_busy":
            return {"status": "unavailable"}
        try:
            engine.apply(when="when_idle")
        except EngineError:
            return {"status": "unavailable"}
        return {"status": "deferred", "work": exc.detail[:8]}
