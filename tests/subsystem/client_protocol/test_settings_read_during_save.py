"""Saved settings read while a save is under way are read whole (B408).

A save of the tools file moves the old file aside, then gives the new one its name. A handshake in
between answered 503 (its approval policy snapshot found no file, or a changed one), the settings
page started from nothing, and on Windows a program reading the file made the save fail half done,
so every later switch waited on a recovery. Each read here lands in the middle of a real save.
"""
from __future__ import annotations

import os
import threading
from types import SimpleNamespace

import pytest

from row_bot import tasks, tool_configuration
from row_bot.api.v1.security import ClientSecurity, current_policy_snapshot
from row_bot.developer import edits
from row_bot.tools import registry

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]


@pytest.fixture
def switches(tmp_path, monkeypatch):
    """One registered tool whose switch the registry saves, in this test's own data folder."""
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(tasks, "_DB_PATH", str(tmp_path / "tasks.db"))  # Where saves record their progress.
    monkeypatch.setattr(tasks, "_SCHEMA_READY_PATH", None)
    path = tool_configuration.configuration_path()
    tool = SimpleNamespace(name="fixture", destructive_tool_names=frozenset({"delete"}), config_schema={},
                           enabled_by_default=True)
    monkeypatch.setattr(registry, "_tools", {"fixture": tool})
    monkeypatch.setattr(registry, "_enabled", {"fixture": True})
    monkeypatch.setattr(registry, "_tool_configs", {})
    monkeypatch.setattr(registry, "_global_config", {})
    monkeypatch.setattr(registry, "_active_config_path", path)
    monkeypatch.setattr(registry, "_invalidate_agent_cache", lambda: None)
    registry.set_enabled("fixture", True)  # Saved once: the next save moves a file aside.
    return path


@pytest.fixture
def paused_save(monkeypatch):
    """Holds the next save of the tools file just after it moved the old file aside, until a reader
    is done or a moment has passed (a reader that rightly waits for the save can't say so)."""
    moved, read = threading.Event(), threading.Event()
    move = edits._rename_edit_no_replace

    def held(source, destination, **descriptors):
        move(source, destination, **descriptors)
        if os.path.basename(os.fsdecode(source)) == "tools_config.json" and not moved.is_set():
            moved.set()
            read.wait(0.3)
    monkeypatch.setattr(edits, "_rename_edit_no_replace", held)

    def switch(value: bool) -> threading.Thread:
        saving = threading.Thread(target=registry.set_enabled, args=("fixture", value))
        saving.start()
        assert moved.wait(5), "the save never moved the file"
        return saving
    return switch, read


def test_the_approval_policy_read_during_a_tool_switch_is_the_one_before_or_after_it(
        switches, paused_save, monkeypatch):
    from row_bot.mcp_client import runtime
    from row_bot.plugins import registry as plugin_registry
    monkeypatch.setattr(plugin_registry, "get_loaded_manifests", lambda: [])
    monkeypatch.setattr(runtime, "_get_effective_config", lambda: {"enabled": False, "servers": {}})
    switch, read = paused_save
    security = ClientSecurity("fixture", policy=current_policy_snapshot)
    before = security.policy_revision
    saving = switch(False)
    try:
        # What every handshake and approval reads; it raised here and the handshake answered 503 (B408).
        during = security.policy_revision
    finally:
        read.set()
        saving.join(5)
    after = security.policy_revision
    assert before != after
    assert during in {before, after}  # Never a policy read with no saved file.
    assert tool_configuration.read_saved().document["tools"]["fixture"] is False
    assert not tool_configuration.recovery_required()


def test_a_settings_page_switch_during_a_tool_save_keeps_both(switches, paused_save):
    from row_bot.application.settings_commands import _apply, _intent

    switch, read = paused_save
    saving = switch(False)
    try:
        # The settings page turns memory off meanwhile: it read no file and saved only its own switch.
        _apply(_intent("0" * 64, "knowledge", "memory_enabled", False), validate=lambda: None)
    finally:
        read.set()
        saving.join(5)
    assert tool_configuration.read_saved().document["tools"] == {"fixture": False, "memory": False}
    assert not tool_configuration.recovery_required()


def test_a_save_under_way_is_not_reported_as_needing_recovery(switches, paused_save):
    from row_bot.application.native_mcp_controls import read_native_mcp_state

    switch, read = paused_save
    saving = switch(False)
    try:
        state = read_native_mcp_state()
    finally:
        read.set()
        saving.join(5)
    assert state.availability != "recovery_required"
    assert state.resource_revision is not None


@pytest.mark.skipif(os.name != "nt", reason="Only Windows refuses to move a file another program has open")
def test_a_switch_saves_while_another_program_reads_the_file(switches, monkeypatch):
    # Python opens files on Windows without FILE_SHARE_DELETE, as many programs do: the move is refused.
    reader = open(switches, "rb")
    waits = []

    def wait(seconds):
        waits.append(seconds)
        reader.close()  # The other program finishes reading while the save waits.
    monkeypatch.setattr(edits.time, "sleep", wait)
    try:
        registry.set_enabled("fixture", False)
    finally:
        reader.close()
    assert waits  # The first move was refused while the file was open.
    assert tool_configuration.read_saved().document["tools"]["fixture"] is False
    assert not tool_configuration.recovery_required()
