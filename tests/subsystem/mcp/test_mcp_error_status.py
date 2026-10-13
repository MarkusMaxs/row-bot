"""An MCP result its server marked isError is an error result, not a success (D2 decision 7).

The ToolMessage gets ``status="error"``: Claude sees ``is_error: true``, and the finalization
fallback, which answers from successful results, leaves it out like any other failed tool.
"""
from __future__ import annotations

import pytest
from langchain_core.messages import ToolMessage

from tests.subsystem.mcp.test_mcp_output_handles import _in_conversation, _long_text, server as _server

server = _server
pytestmark = [pytest.mark.subsystem, pytest.mark.mcp_transport]


@pytest.fixture
def discovered(server, monkeypatch):
    """The "notes" app's read_page tool, as discovery records it."""
    from row_bot.mcp_client import runtime

    info = runtime.McpToolInfo(server_name="notes", name="read_page", prefixed_name="mcp_notes_read_page")
    monkeypatch.setitem(runtime._catalog, "notes", {"read_page": info})
    return server


def _created(text: str, name: str = "mcp_notes_read_page") -> ToolMessage:
    from row_bot import tool_outputs

    return _in_conversation(tool_outputs.cap_tool_message, ToolMessage(content=text, name=name, tool_call_id="call-1"))


def test_an_mcp_error_result_is_an_error_for_the_model_and_the_fallback(discovered):
    import row_bot.agent as agent
    from langchain_anthropic.chat_models import _format_messages
    from row_bot.mcp_client import runtime

    discovered("The page does not exist.", error=True)
    seen = _in_conversation(runtime._make_tool_func("notes", "read_page"), page="missing")
    created = _created(seen)

    assert created.status == "error"
    assert created.content == "MCP tool error: The page does not exist."
    assert agent._tool_message_successful(created) is False
    _system, formatted = _format_messages([created])
    assert formatted[-1]["content"][0]["is_error"] is True


def test_a_successful_mcp_result_stays_a_success(discovered):
    import row_bot.agent as agent
    from row_bot.mcp_client import runtime

    discovered("Three notes found.")
    created = _created(_in_conversation(runtime._make_tool_func("notes", "read_page"), page="all"))

    assert created.status == "success"
    assert agent._tool_message_successful(created) is True


def test_a_long_mcp_error_shortened_with_a_handle_is_still_an_error(discovered):
    from row_bot.mcp_client import runtime

    discovered(_long_text(), error=True, output_limit=24_000)
    seen = _in_conversation(runtime._make_tool_func("notes", "read_page"), page="all")

    assert seen.startswith("MCP tool error: [mcp_notes_read_page output is ")
    assert _created(seen).status == "error"


def test_an_error_after_rowbots_approval_line_is_still_an_error(discovered):
    created = _created("Approval: not needed (read-only)\nMCP tool error: The page does not exist.")

    assert created.status == "error"


def test_a_look_alike_tool_cannot_mark_its_result_as_an_mcp_error(discovered):
    # Not a tool this MCP runtime issued: its text is only text.
    assert _created("MCP tool error: something", name="mcp_other_lookalike").status == "success"
    assert _created("MCP tool error: something", name="calculator").status == "success"
