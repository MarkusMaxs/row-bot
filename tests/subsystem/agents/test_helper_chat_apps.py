"""A helper keeps the apps of the chat that delegated it; a read-only helper only reads them (B344).

The built-in profiles list their tools and none listed the apps, so a research helper delegated
from a chat with Notion on reported it "couldn't access Notion under its tool scope". These are
security tests (a read-only helper must never change an app): they stay in the pull request lane.
"""
from __future__ import annotations

import pytest

from tests.subsystem.agents.test_agent_tool_filtering import _lc_tool
from tests.subsystem.agents.test_helper_code_folder_reads import _modules

pytestmark = pytest.mark.subsystem

_CHAT_TOOLS = ["mcp", "memory", "conversation_search", "agents", "row_bot_status", "url_reader"]


@pytest.fixture
def notion(monkeypatch):
    """Notion's tools as the MCP runtime classified them when the app was set up."""
    from row_bot.mcp_client import runtime

    catalog = {"Notion": {
        "search": runtime.McpToolInfo("Notion", "search", "mcp_notion_search", enabled=True, effect="read_only"),
        "create_page": runtime.McpToolInfo("Notion", "create_page", "mcp_notion_create_page", enabled=True,
                                           effect="mutation"),
    }}
    monkeypatch.setattr(runtime, "_catalog", catalog)
    return [_lc_tool(info.prefixed_name) for info in catalog["Notion"].values()]


def _helper_calls(observed: dict, tools):
    """Inside the helper's own run: each Notion tool through the dispatch guard, as its graph binds it."""

    def invoke(prompt, enabled_tool_names, config, *, stop_event):
        from row_bot import agent

        configurable = config["configurable"]
        agent._set_active_runtime_context(
            thread_id=configurable["thread_id"],
            runtime_surface=configurable["runtime_surface"],
            approval_mode=configurable["approval_mode"],
            tool_allowlist=configurable.get("tool_allowlist"),
            agent_profile_id=configurable["agent_profile_id"],
            agent_profile_snapshot=configurable["agent_profile_snapshot"],
            agent_profile_frozen=True,
            agent_run_id=configurable["agent_run_id"],
        )
        observed["tools"] = list(enabled_tool_names)
        observed["allowlist"] = list(configurable.get("tool_allowlist") or [])
        for tool in tools:
            bound = agent._bind_profile_tool(tool, source="mcp", parent="mcp")
            observed[tool.name] = str(bound.invoke({"query": "Q4 plan"}))
        return "Found it."

    return invoke


@pytest.mark.parametrize("approval_mode", ["auto", "full"])
def test_a_read_only_helper_reads_the_chats_apps_and_changes_nothing(
    tmp_path, monkeypatch, reload_for_data_dir, notion, approval_mode
):
    agent_runner, threads, _profiles = _modules(tmp_path, monkeypatch, reload_for_data_dir)
    chat = threads.create_thread("Month-end close", approval_mode=approval_mode)
    observed: dict = {}
    monkeypatch.setattr(agent_runner, "_invoke_agent", _helper_calls(observed, notion))

    run = agent_runner.spawn_agent_run(
        "Find the right Notion location.", parent_thread_id=chat, profile="research",
        enabled_tool_names=_CHAT_TOOLS, wait=True, timeout=10,
    )

    assert run["status"] == "completed"
    assert "mcp" in observed["tools"] and "mcp" in observed["allowlist"]
    assert observed["mcp_notion_search"] == "mcp_notion_search:Q4 plan"
    # Not even Hands-off lets a read-only helper change an app.
    assert observed["mcp_notion_create_page"].startswith("BLOCKED")
    assert "read-only Agent Profile" in observed["mcp_notion_create_page"]


def test_a_helper_from_a_chat_without_apps_gains_none(tmp_path, monkeypatch, reload_for_data_dir, notion):
    agent_runner, threads, _profiles = _modules(tmp_path, monkeypatch, reload_for_data_dir)
    chat = threads.create_thread("Notes")
    observed: dict = {}
    monkeypatch.setattr(agent_runner, "_invoke_agent", _helper_calls(observed, []))

    agent_runner.spawn_agent_run(
        "Summarise.", parent_thread_id=chat, profile="research",
        enabled_tool_names=[name for name in _CHAT_TOOLS if name != "mcp"], wait=True, timeout=10,
    )

    assert "mcp" not in observed["tools"] and "mcp" not in observed["allowlist"]


def test_a_person_s_own_profile_keeps_its_tool_list(tmp_path, monkeypatch, reload_for_data_dir, notion):
    agent_runner, threads, agent_profiles = _modules(tmp_path, monkeypatch, reload_for_data_dir)
    chat = threads.create_thread("Notes")
    agent_profiles.save_agent_profile(
        slug="notes_only", display_name="Notes only", instructions="Read notes.",
        tool_policy_json={"capability": "read_only", "allow_tools": ["memory"]},
    )
    observed: dict = {}
    monkeypatch.setattr(agent_runner, "_invoke_agent", _helper_calls(observed, notion))

    agent_runner.spawn_agent_run(
        "Summarise.", parent_thread_id=chat, profile="notes_only",
        enabled_tool_names=_CHAT_TOOLS, wait=True, timeout=10,
    )

    assert "mcp" not in observed["tools"]
    assert observed["mcp_notion_search"].startswith("BLOCKED")
