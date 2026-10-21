"""Real calls through the person's own Claude Code and Claude plan (N1). Off by default.

On a computer with Claude Code installed and signed in to a Claude plan:

    ROW_BOT_LIVE_PROVIDER_E2E=1 uv run python -m pytest tests/e2e/test_claude_plan_live.py -m live_provider

Two short calls on the plan's limits (a reply and a tool call), in the tests' isolated data folder. The model
is ``ROW_BOT_LIVE_CLAUDE_MODEL`` (default Claude Haiku 4.5, the cheapest Claude Code lists).
"""

from __future__ import annotations

import os

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool

pytestmark = [pytest.mark.e2e, pytest.mark.live_provider]


def test_a_reply_and_a_tool_call_through_the_installed_claude_code():
    if os.environ.get("ROW_BOT_LIVE_PROVIDER_E2E", "").strip().lower() not in {"1", "true", "yes"}:
        pytest.skip("Set ROW_BOT_LIVE_PROVIDER_E2E=1 to run real Claude plan calls.")
    from row_bot.providers.claude_subscription import claude_plan_status, unavailable_message
    from row_bot.providers.transports.claude_plan_cli import ChatClaudePlan

    status = claude_plan_status(fresh=True)
    if not status.ready:
        pytest.skip(unavailable_message(status))
    model = ChatClaudePlan(model_name=os.environ.get("ROW_BOT_LIVE_CLAUDE_MODEL", "claude-haiku-4-5-20251001"))

    reply = model.invoke([HumanMessage(content="Reply with exactly this text and nothing else: row-bot-claude-ok")])

    assert "row-bot-claude-ok" in reply.content
    assert reply.response_metadata["stop_reason"] == "end_turn"
    assert reply.usage_metadata["input_tokens"] > 0

    def get_weather(city: str) -> str:
        """Get today's weather for a city."""
        raise AssertionError("Row-Bot runs tools itself, after its approval gate; the test never does.")

    called = model.bind_tools([StructuredTool.from_function(get_weather)]).invoke(
        [HumanMessage(content="What's the weather in Paris today? Use the get_weather tool.")])

    assert [(call["name"], call["args"].get("city")) for call in called.tool_calls] == [("get_weather", "Paris")]
    assert called.response_metadata["stop_reason"] == "tool_use"
