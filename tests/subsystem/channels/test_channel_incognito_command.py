"""``/incognito on|off`` in a channel chat does what the chat's Incognito switch does in the app (N11)."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.subsystem


def test_incognito_command_turns_the_channel_chat_incognito_and_back(tmp_path, reload_for_data_dir):
    threads, commands = reload_for_data_dir(tmp_path, "row_bot.threads", "row_bot.channels.commands")
    threads.create_thread("Telegram chat", thread_id="tg-1", seed_default_skills=False)

    assert commands.is_thread_scoped_command("/incognito on")
    assert "Incognito is off here" in commands.dispatch("telegram", "/incognito", thread_id="tg-1")

    on = commands.dispatch("telegram", "/incognito on", thread_id="tg-1")
    assert on.startswith("Incognito is on: nothing in this chat is saved to memory unless you say so.")
    assert threads.incognito_state("tg-1").incognito is True
    assert "Incognito is on here" in commands.dispatch("telegram", "/incognito", thread_id="tg-1")

    off = commands.dispatch("telegram", "/incognito OFF", thread_id="tg-1")
    assert off.startswith("Incognito is off")
    state = threads.incognito_state("tg-1")
    assert state.incognito is False and state.boundary is not None


def test_incognito_command_needs_the_chat_and_a_clear_choice(tmp_path, reload_for_data_dir):
    threads, commands = reload_for_data_dir(tmp_path, "row_bot.threads", "row_bot.channels.commands")
    threads.create_thread("Slack chat", thread_id="slack-1", seed_default_skills=False)

    assert "could not identify" in commands.dispatch("slack", "/incognito on")
    assert commands.dispatch("slack", "/incognito maybe", thread_id="slack-1").startswith("Usage:")
    assert threads.incognito_state("slack-1").incognito is False
