"""B386: a helper thinks as its conversation does, unless it is given its own level."""
from __future__ import annotations

import importlib
import json

import pytest

# Fixed, offline reasoning levels: grok-4.6 offers low/medium/high/xhigh, grok-3-mini low/high.
CHAT_MODEL = "model:xai:grok-4.6"
SMALL_MODEL = "model:xai:grok-3-mini"


@pytest.fixture
def runner(tmp_path, monkeypatch, reload_for_data_dir):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    skills_activation = importlib.import_module("row_bot.skills_activation")
    monkeypatch.setattr(skills_activation, "DATA_DIR", data_dir)
    monkeypatch.setattr(skills_activation, "STATE_PATH", data_dir / "skills_activation.json")
    _tasks, threads, _settings, _profiles, agent_runs, _context, agent_runner = reload_for_data_dir(
        data_dir,
        "row_bot.tasks",
        "row_bot.threads",
        "row_bot.agent_settings",
        "row_bot.agent_profiles",
        "row_bot.agent_runs",
        "row_bot.agent_context",
        "row_bot.agent_runner",
    )
    from row_bot.providers import reasoning

    seen: dict[str, str] = {}

    def fake_invoke(prompt, enabled_tool_names, config, *, stop_event):
        # What the helper's own turns ask the model for.
        configurable = config["configurable"]
        plan = reasoning.request_plan_for(configurable["thread_id"], configurable["model_override"])
        seen["reasoning"] = plan.selection.label
        return "done"

    monkeypatch.setattr(agent_runner, "_invoke_agent", fake_invoke)
    return agent_runner, agent_runs, threads, seen


def _chat(threads, level: str) -> str:
    thread_id = threads.create_thread("Chat", model_override=CHAT_MODEL)
    threads.set_thread_reasoning_selection(thread_id, CHAT_MODEL, {"kind": "effort", "effort": level})
    return thread_id


def test_a_helper_from_a_chat_set_to_high_thinks_at_high(runner) -> None:
    agent_runner, _runs, threads, seen = runner
    chat = _chat(threads, "high")

    run = agent_runner.spawn_agent_run("Check the dates.", parent_thread_id=chat, wait=True)

    assert run["status"] == "completed"
    assert seen["reasoning"] == "High"
    assert agent_runner.thinking_line(agent_runner.helper_thinking(run["id"])) == "grok-4.6 · High"
    # Changing the chat's level later is for new helpers, not this one.
    threads.set_thread_reasoning_selection(chat, CHAT_MODEL, {"kind": "effort", "effort": "low"})
    from row_bot.providers import reasoning

    assert reasoning.request_plan_for(run["thread_id"], CHAT_MODEL).selection.label == "High"


def test_a_level_given_to_the_helper_wins(runner) -> None:
    agent_runner, _runs, threads, seen = runner
    chat = _chat(threads, "high")

    agent_runner.spawn_agent_run("Summarise.", parent_thread_id=chat, reasoning="low", wait=True)

    assert seen["reasoning"] == "Low"


def test_a_level_the_helpers_model_lacks_is_refused_with_its_choices(runner) -> None:
    agent_runner, agent_runs, threads, _seen = runner
    chat = _chat(threads, "high")

    with pytest.raises(agent_runner.HelperReasoningError) as refused:
        agent_runner.spawn_agent_run(
            "Summarise.", parent_thread_id=chat, model_override=SMALL_MODEL, reasoning="xhigh",
        )

    assert "Valid choices for model:xai:grok-3-mini: default, low, high." in str(refused.value)
    # Refused before anything started.
    assert agent_runs.list_child_runs(parent_thread_id=chat) == []


def test_another_model_keeps_the_chats_level_when_it_offers_it(runner) -> None:
    agent_runner, _runs, threads, seen = runner

    run = agent_runner.spawn_agent_run(
        "Summarise.", parent_thread_id=_chat(threads, "high"), model_override=SMALL_MODEL, wait=True,
    )

    assert seen["reasoning"] == "High"
    assert agent_runner.helper_thinking(run["id"])["not_offered"] == ""


def test_another_model_without_the_chats_level_uses_its_default_and_says_so(runner) -> None:
    agent_runner, _runs, threads, seen = runner

    run = agent_runner.spawn_agent_run(
        "Summarise.", parent_thread_id=_chat(threads, "medium"), model_override=SMALL_MODEL, wait=True,
    )

    assert seen["reasoning"] == "Provider default"
    assert (agent_runner.thinking_line(agent_runner.helper_thinking(run["id"]))
            == "grok-3-mini · Provider default: Medium isn't offered")


def test_agent_command_takes_reasoning_like_the_reasoning_command(runner) -> None:
    from row_bot.agent_commands import parse_agent_spawn_text

    assert parse_agent_spawn_text("/agent --reasoning low review the plan").reasoning == "low"
    assert parse_agent_spawn_text("/subagent --reasoning=high review").reasoning == "high"
    assert parse_agent_spawn_text("/agent --reasoning budget 4000 review").reasoning == "budget 4000"
    assert parse_agent_spawn_text("/agent review the plan").reasoning == ""
    # A missing level is not a task.
    assert parse_agent_spawn_text("/agent --reasoning") is None


def test_delegate_work_passes_its_reasoning_and_says_how_the_helper_thinks(
    tmp_path, monkeypatch, reload_for_data_dir,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    *_, agent_runs, agent_runner, agent_tool = reload_for_data_dir(
        data_dir,
        "row_bot.tasks",
        "row_bot.threads",
        "row_bot.agent_profiles",
        "row_bot.agent_runs",
        "row_bot.agent_runner",
        "row_bot.tools.agent_tool",
    )
    import row_bot.models as models

    monkeypatch.setattr(models, "_current_model", CHAT_MODEL)
    calls: dict = {}

    def fake_spawn(objective, **kwargs):
        calls.update(kwargs)
        run = agent_runs.create_agent_run(parent_thread_id=kwargs["parent_thread_id"], prompt=objective)
        agent_runs.append_agent_event(
            run["id"], "reasoning.chosen", {"model": "grok-4.6", "reasoning": "Low", "not_offered": ""},
        )
        return run

    monkeypatch.setattr(agent_tool.agent_runner, "spawn_agent_run", fake_spawn)

    payload = json.loads(agent_tool._delegate_work(
        objective="Summarise.", reasoning="low", parent_thread_id="parent-thread", wait=False,
    ))

    assert payload["ok"] is True
    assert calls["reasoning"] == "low"
    assert "It runs on grok-4.6 · Low." in payload["message"]
