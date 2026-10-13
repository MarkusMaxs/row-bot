"""A tool result rewritten for the model keeps its status, artifact, id and kwargs (F22).

The Agent rewrites tool results on their way to the model: inline data URIs are redacted, untrusted
text is fenced, old output is cleared and repeats are stubbed. Each rewrite used to build a fresh
ToolMessage from name, call id and content only, so an error result reached the model as a success
(Anthropic sends ``status="error"`` as ``is_error``) and its artifact and id were lost.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import ToolMessage

import row_bot.agent as agent
from tests.helpers.agent_requests import AgentConversation, text_of_tokens

pytestmark = pytest.mark.subsystem


@pytest.fixture(autouse=True)
def thread_db(tmp_path, monkeypatch):
    from row_bot import threads

    monkeypatch.setattr(threads, "DB_PATH", str(tmp_path / "threads.db"))


def _model_view(request: list, call_id: str) -> ToolMessage:
    return next(message for message in request if isinstance(message, ToolMessage) and message.tool_call_id == call_id)


def test_fenced_and_redacted_error_results_keep_their_fields(monkeypatch) -> None:
    conversation = AgentConversation(agent, monkeypatch)
    conversation.say("Fetch the page.")
    call = conversation.tool(
        "mcp_notes_read_page",
        "Page body. data:image/png;base64," + "A" * 400,
        status="error",
        artifact={"raw": "kept"},
        additional_kwargs={"platform_media": [{"type": "media.error"}]},
    )

    message = _model_view(conversation.request(), call)

    assert message.content.startswith('<EXTERNAL_CONTENT source="mcp_notes_read_page">')
    assert "[inline image/png stripped" in message.content
    assert (message.status, message.artifact, message.id) == ("error", {"raw": "kept"}, "tool-1")
    assert message.additional_kwargs["platform_media"] == [{"type": "media.error"}]
    stored = next(item for item in conversation.state["messages"] if isinstance(item, ToolMessage))
    assert stored.content.startswith("Page body. data:image/png")  # the checkpoint keeps what the tool returned


def test_cleared_and_repeated_results_keep_their_status(monkeypatch) -> None:
    conversation = AgentConversation(agent, monkeypatch, context_window=40_000)
    failed = []
    for number in range(1, 9):
        conversation.say(f"Read part {number}.")
        failed.append(conversation.tool("read_file", text_of_tokens(f"part {number}", 3_000), status="error"))
        conversation.answer("Done.")
    conversation.say("Read part 8 again.")
    repeat = conversation.tool("read_file", text_of_tokens("part 8", 3_000), status="error")

    request = conversation.request()

    cleared = _model_view(request, failed[0])
    assert cleared.content.startswith("[read_file]: ") and cleared.status == "error"
    stub = _model_view(request, repeat)
    assert stub.content.startswith("[Identical to the result of call") and stub.status == "error"


def test_claude_is_told_an_error_result_is_an_error(monkeypatch) -> None:
    from row_bot.providers.transports.anthropic_cancellable import CancellableChatAnthropic

    conversation = AgentConversation(agent, monkeypatch)
    conversation.say("Fetch the page.")
    conversation.tool("mcp_notes_read_page", "Server error: page not found.", status="error")

    payload = CancellableChatAnthropic(model="claude-sonnet-4-5", api_key="test")._get_request_payload(
        conversation.request()
    )

    results = [
        block
        for message in payload["messages"]
        for block in (message["content"] if isinstance(message["content"], list) else [])
        if block.get("type") == "tool_result"
    ]
    assert [block.get("is_error") for block in results] == [True]
