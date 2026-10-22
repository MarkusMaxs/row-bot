"""A turn that has ended always says so, so its row stops spinning (B396).

The open conversation's sidebar row spins while the conversation's last generation state says a reply
is being written. A turn ends by publishing a settled state. A step before that (reading the saved
reply in, recording the turn, retiring the live reply, the goal after the turn) that failed left the
last state at running though nothing ran any more: the row kept spinning, and a reload showed it too.
"""
from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

import pytest
from langchain_core.messages import AIMessage

from tests.contracts.client_platform.test_headless_lifecycle import platform, submit  # noqa: F401
from tests.helpers.client_platform_fakes import CheckpointCommit, ScriptedAgentStream, fixture_id

pytestmark = pytest.mark.subsystem


def _reply(label: str, *, saved: bool = True) -> ScriptedAgentStream:
    """A reply that is saved, or one the provider cut off part-way (its live text is left to retire)."""
    if not saved:
        return ScriptedAgentStream((("token", "Half a"), ("error", "Synthetic provider failure")))
    native_id = fixture_id(label + ":reply")
    return ScriptedAgentStream((("token", "All done."),
                                CheckpointCommit((AIMessage(content="All done.", id=native_id),), native_id),
                                ("done", "All done.")))


def _assert_settled(platform) -> None:  # noqa: F811
    generation = platform.snapshot("conversation-a")["generation"]
    assert generation["status"] not in {"running", "stopping"}
    assert generation["quiesced"] is True
    assert generation["can_stop"] is False
    assert platform.get_conversation("conversation-a")["generation_state"] == []


def _fails_once(error: Exception, when: Callable[[], bool]):
    """Wrap a step so it fails once, the first time it runs once ``when`` holds (a busy database, a
    locked file)."""
    failed: list[bool] = []

    def broken(original):
        def step(*args, **kwargs):
            if not failed and when():
                failed.append(True)
                raise error
            return original(*args, **kwargs)
        return step
    return broken


@pytest.mark.parametrize("step", ["saved reply", "turn record", "live reply"])
def test_a_turn_whose_cleanup_fails_still_ends(platform, monkeypatch, caplog, step):  # noqa: F811
    from row_bot.application import live_content
    from row_bot.runtime import admissions

    fake = _reply("cleanup-" + step.replace(" ", "-"), saved=step != "live reply")
    broken = _fails_once(sqlite3.OperationalError("database is locked"), fake.quiesced.is_set)
    if step == "saved reply":
        monkeypatch.setattr(platform, "_refresh_checkpoint", broken(platform._refresh_checkpoint))
    elif step == "turn record":
        monkeypatch.setattr(admissions, "finish", broken(admissions.finish))
    else:
        monkeypatch.setattr(live_content, "discard", broken(live_content.discard))

    try:
        with caplog.at_level(logging.WARNING, logger="row_bot.application.client_platform"):
            handle = platform.registry.get(submit(platform, fake, "cleanup-" + step)["execution_id"])
            assert handle.producer_done.wait(10)

        _assert_settled(platform)
        assert "database is locked" in caplog.text
    finally:
        # The failed discard left its live text behind in the process-wide store.
        for reference in live_content.references("conversation-a"):
            live_content.discard("conversation-a", reference)


@pytest.mark.parametrize("when", ["before the reply", "after the reply"])
def test_a_turn_ends_when_its_goal_cannot_be_read(platform, monkeypatch, when):  # noqa: F811
    from row_bot import goals

    fake = _reply("goal-" + when.replace(" ", "-"))
    ended = fake.quiesced.is_set if when == "after the reply" else (lambda: True)
    monkeypatch.setattr(goals, "get_current_goal", _fails_once(
        sqlite3.OperationalError("database is locked"), ended)(goals.get_current_goal))

    handle = platform.registry.get(submit(platform, fake, "goal-" + when)["execution_id"])
    assert handle.producer_done.wait(10)

    _assert_settled(platform)
