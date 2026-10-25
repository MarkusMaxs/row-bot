"""A paused turn goes on with the answer bound to its own interrupt."""

from __future__ import annotations


def test_single_interrupt_resume_is_bound_to_its_langgraph_id(monkeypatch) -> None:
    import row_bot.agent as agent

    inputs = []
    fake_agent = type(
        "FakeAgent",
        (),
        {
            "get_state": lambda self, _config: type("State", (), {"values": {}})(),
            "update_state": lambda self, _config, _values: None,
        },
    )()
    monkeypatch.setattr(agent, "get_agent_graph", lambda *_args, **_kwargs: fake_agent)

    def fake_stream(_graph, input_data, _config, **_kwargs):
        inputs.append(input_data)
        yield ("done", "resumed")

    monkeypatch.setattr(agent, "_stream_graph", fake_stream)

    events = list(
        agent.resume_stream_agent(
            [],
            {
                "configurable": {
                    "thread_id": "thread",
                    "generation_id": "generation",
                }
            },
            True,
            interrupt_ids=["interrupt-one"],
        )
    )

    assert events == [("done", "resumed")]
    assert inputs[0].resume == {"interrupt-one": True}


def test_non_streaming_resume_without_root_objective_uses_empty_fallback(monkeypatch) -> None:
    import row_bot.agent as agent

    state = type("State", (), {"next": (), "tasks": (), "values": {}})()
    fake_agent = type(
        "FakeAgent",
        (),
        {
            "stream": lambda self, *_args, **_kwargs: (),
            "get_state": lambda self, _config: state,
        },
    )()
    monkeypatch.setattr(agent, "get_agent_graph", lambda *_args, **_kwargs: fake_agent)
    monkeypatch.setattr(
        agent,
        "_resume_agent_graph_config",
        lambda _graph, config: config,
    )

    result = agent.resume_invoke_agent(
        [],
        {
            "configurable": {
                "thread_id": "thread",
                "generation_id": "generation",
            }
        },
        True,
        interrupt_ids=["interrupt-one"],
    )

    assert result == "I wasn't able to generate a response."
    assert agent.get_active_runtime_context()["root_objective"] == ""
