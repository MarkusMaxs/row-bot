"""Stop over the client API ends a chat turn whose cloud model hasn't answered yet, and the turn ends as stopped.

The turn's model call runs the Agent graph with a cloud chat model, built as the runtime builds it, against a
provider on loopback that never answers. Stop must end the request at once (the provider sees the client go) and
the turn as stopped rather than failed.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent

from tests.helpers.fake_provider_http import HOLD, fake_provider_http
from tests.subsystem.client_protocol.test_protocol_application import (
    _client,
    _command,
    service,  # noqa: F401
)
from tests.subsystem.client_protocol.test_protocol_security import bootstrap

pytestmark = pytest.mark.subsystem

MODEL = "claude-sonnet-4-5"
# Stop ends the call at once; the bound only keeps a broken test from hanging.
PROMPTLY = 2.0


def _calculate(expression: str) -> str:
    return "4"


def test_stop_ends_a_turn_whose_cloud_model_has_not_answered(service, monkeypatch):  # noqa: F811
    import row_bot.agent as agent
    from row_bot.providers import runtime

    notices: list = []
    monkeypatch.setattr("row_bot.notifications.notify", lambda *args, **kwargs: notices.append(args))
    monkeypatch.setattr(runtime, "get_provider_secret", lambda *_args, **_kwargs: "test-key")
    with fake_provider_http([HOLD]) as server:
        monkeypatch.setenv("ANTHROPIC_API_URL", server.url)
        model = runtime.create_chat_model(MODEL, provider_id="anthropic")
        tool = StructuredTool.from_function(_calculate, name="calculate", description="Calculate.")
        graph = create_react_agent(model=model, tools=[tool], checkpointer=InMemorySaver())

        def stream(text, enabled, config, *, stop_event=None):
            return agent._stream_graph(graph, {"messages": [HumanMessage(content=text)]},
                                       {"configurable": {"thread_id": config["configurable"]["thread_id"]}},
                                       stop_event=stop_event)

        service.stream_factory = stream
        with _client(service) as client:
            _, headers = bootstrap(client)
            created = _command(client, headers, "conversation.create", {"title": "Cloud model"})
            conversation = created.json()["conversation_id"]
            submitted = _command(client, headers, "conversation.submit", {
                "submission_id": str(uuid4()), "text": "What is 2 + 2?", "attachment_refs": [],
                "model_selection": {"provider_id": "anthropic", "model_ref": f"model:anthropic:{MODEL}"},
            }, target=conversation, revision=service.get_conversation(conversation)["revision"])
            assert submitted.status_code == 202, submitted.text
            handle = service.registry.get(submitted.json()["execution_id"])
            held = server.held(0)

            stopped = _command(client, headers, "conversation.stop", {"generation_id": handle.generation_id},
                               target=conversation, revision=service.get_conversation(conversation)["revision"])
            assert stopped.status_code == 200, stopped.text

            assert handle.producer_done.wait(PROMPTLY)
            assert handle.status == "stopped"
            assert held.hung_up.wait(PROMPTLY)
            assert len(server.seen) == 1
            events = service.events_since(conversation)["events"]
            assert not [event for event in events if event.get("type") == "generation.error"]
            assert notices == []
