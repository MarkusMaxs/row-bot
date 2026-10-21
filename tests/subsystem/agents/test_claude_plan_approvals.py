"""A tool call from Claude (your Claude plan) goes through Row-Bot's approval gate like any provider's (N1).

Security: Claude Code's own tools are off and it refuses Row-Bot's, so every tool call it reports comes
back to Row-Bot's agent loop, which asks first where the approval level says so. A fake ``claude``
replays a recorded tool call; the agent graph, the approval gate and the transport are real.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool

from tests.helpers.fake_claude import install_fake_claude, tool_reply
from tests.subsystem.providers.test_provider_settings_controls import store as store  # noqa: F401

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]


def test_an_ask_level_tool_claude_calls_waits_for_the_persons_approval(store, tmp_path, monkeypatch):
    from langgraph.checkpoint.memory import MemorySaver

    import row_bot.agent as agent
    from row_bot.providers.claude_subscription import forget_claude_plan_status
    from row_bot.providers.transports.claude_plan_cli import ChatClaudePlan

    ran: list[str] = []

    def delete_note(title: str) -> str:
        """Delete one of the person's notes."""
        ran.append(title)
        return "deleted"

    tool = StructuredTool.from_function(delete_note)
    agent._wrap_with_interrupt_gate(tool)
    token = agent._approval_mode_var.set("approve")  # Ask
    install_fake_claude(tmp_path, monkeypatch, reply=tool_reply("delete_note", {"title": "Groceries"}))
    graph = agent.create_react_agent(model=ChatClaudePlan(model_name="claude-sonnet-5"), tools=[tool],
                                     checkpointer=MemorySaver())
    config = {"configurable": {"thread_id": "claude-plan-approval"}}
    try:
        graph.invoke({"messages": [HumanMessage(content="Delete my groceries note.")]}, config)
        state = graph.get_state(config)
    finally:
        agent._approval_mode_var.reset(token)
        forget_claude_plan_status()

    asked = [item.value for task in state.tasks for item in task.interrupts]
    assert [question["tool"] for question in asked] == ["delete_note"]
    assert "Groceries" in asked[0]["description"]
    assert ran == []  # it waits for the person; neither Claude Code nor Row-Bot ran it
