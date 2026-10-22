"""A cloud model call that Stop ends is a stopped turn, not an API error.

Stop shuts the provider request's socket down at once, waiting for headers or mid-stream, so the model call ends
with an error (the SDK's stopped error, or the transport's nothing-came-back). The turn must end as stopped: no
error, no "API Error" notification, and nothing of the stopped reply saved. These run the Agent graph with cloud
chat models (an SDK one built as the runtime builds it, and one of Row-Bot's own transports) against a provider
on loopback that never answers.
"""

from __future__ import annotations

import contextvars
import threading

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.tools import StructuredTool
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.prebuilt import create_react_agent

from row_bot.cancellation import CancellationScope, use_cancellation_scope
from tests.helpers.fake_provider_http import HOLD, fake_provider_http

pytestmark = pytest.mark.subsystem

QUESTION = "What is 2 + 2?"
# Stop ends the call at once; the bound only keeps a broken test from hanging.
PROMPTLY = 2.0


def _calculate(expression: str) -> str:
    return "4"


CALCULATE = StructuredTool.from_function(_calculate, name="calculate", description="Calculate.")


def _anthropic(url: str, monkeypatch):
    from row_bot.providers import runtime

    monkeypatch.setattr(runtime, "get_provider_secret", lambda *_args, **_kwargs: "test-key")
    monkeypatch.setenv("ANTHROPIC_API_URL", url)
    return runtime.create_chat_model("claude-sonnet-4-5", provider_id="anthropic")


def _openai_compatible(url: str, _monkeypatch):
    from row_bot.providers.transports.openai_compatible import ChatOpenAICompatible

    endpoint = {"provider_id": "custom_stop_test", "base_url": f"{url}/v1", "transport": "openai_chat"}
    return ChatOpenAICompatible(model_name="m", api_key="k", base_url=f"{url}/v1", endpoint=endpoint)


MODELS = {"anthropic": _anthropic, "openai_compatible": _openai_compatible}


@pytest.fixture
def notices(monkeypatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr("row_bot.notifications.notify", lambda title, message, *args, **kwargs: seen.append((title, message)))
    return seen


def _under_scope(run, scope: CancellationScope) -> tuple[threading.Thread, dict]:
    """Run *run* on another thread under *scope*, as a turn's producer does."""
    outcome: dict = {}

    def owned() -> None:
        with use_cancellation_scope(scope):
            try:
                outcome["result"] = run()
            except BaseException as error:  # noqa: BLE001 - the test reads it
                outcome["error"] = error

    worker = threading.Thread(target=contextvars.copy_context().run, args=(owned,), daemon=True)
    worker.start()
    return worker, outcome


@pytest.mark.parametrize("name", MODELS)
def test_stop_during_a_cloud_call_ends_the_turn_as_stopped(name: str, monkeypatch, notices) -> None:
    import row_bot.agent as agent

    with fake_provider_http([HOLD]) as server:
        graph = create_react_agent(model=MODELS[name](server.url, monkeypatch), tools=[CALCULATE],
                                   checkpointer=InMemorySaver())
        config = {"configurable": {"thread_id": f"stopped-{name}-call"}}
        scope = CancellationScope()
        worker, outcome = _under_scope(lambda: list(agent._stream_graph(
            graph, {"messages": [HumanMessage(content=QUESTION)]}, config, stop_event=scope.stop_event)), scope)
        held = server.held(0)

        scope.cancel("user")
        worker.join(PROMPTLY)

        assert not worker.is_alive()
        assert "error" not in outcome
        assert [kind for kind, *_ in outcome["result"] if kind == "error"] == []
        assert notices == []
        assert held.hung_up.wait(PROMPTLY)
        assert len(server.seen) == 1
        assert [message.type for message in graph.get_state(config).values["messages"]] == ["human"]


def test_a_workflow_step_stopped_during_a_cloud_call_is_stopped(monkeypatch, notices) -> None:
    import row_bot.agent as agent

    with fake_provider_http([HOLD]) as server:
        graph = create_react_agent(model=_anthropic(server.url, monkeypatch), tools=[CALCULATE],
                                   checkpointer=InMemorySaver())
        monkeypatch.setattr(agent, "get_agent_graph", lambda *args, **kwargs: graph)
        monkeypatch.setattr(agent, "get_context_size", lambda *args, **kwargs: 200_000)
        monkeypatch.setattr(agent, "_new_agent_graph_input",
                            lambda text, config, agent=None: (config, {"messages": [HumanMessage(content=text)]}))
        config = {"configurable": {"thread_id": "stopped-workflow-step",
                                   "model_override": "model:anthropic:claude-sonnet-4-5"}}
        scope = CancellationScope()
        worker, outcome = _under_scope(lambda: agent.invoke_agent(
            QUESTION, ["calculate"], config, stop_event=scope.stop_event), scope)
        held = server.held(0)

        scope.cancel("user")
        worker.join(PROMPTLY)

        assert not worker.is_alive()
        assert isinstance(outcome.get("error"), agent.TaskStoppedError)
        assert notices == []
        assert held.hung_up.wait(PROMPTLY)
