"""A helper started from an incognito chat is incognito too (N11): its own thread is never read for memory."""
from __future__ import annotations

import pytest

from tests.subsystem.agents.test_agent_runner import _fresh_agent_runner_modules

pytestmark = pytest.mark.subsystem


@pytest.mark.parametrize("incognito", [True, False])
def test_a_helper_inherits_incognito_from_the_chat_it_was_started_from(
    tmp_path, monkeypatch, reload_for_data_dir, incognito
):
    agent_runner, _runs, _profiles, _context, threads = _fresh_agent_runner_modules(
        tmp_path, monkeypatch, reload_for_data_dir)
    parent = threads.create_thread("Parent", model_override="model:test", incognito=incognito)
    monkeypatch.setattr(agent_runner, "_invoke_agent",
                        lambda prompt, enabled_tool_names, config, *, stop_event: "done")

    run = agent_runner.spawn_agent_run("Look into it.", parent_thread_id=parent, wait=True)

    assert run["status"] == "completed"
    assert threads.incognito_state(run["thread_id"]).incognito is incognito
