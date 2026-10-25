"""B417: a workflow step's "Always ask me" through the real Agent graph and its SQLite checkpoints.

The shell's run_command takes its run's config, as LangChain hands it to a tool that asks for it.
The person's card held that config, which a checkpoint can't save: the run crashed instead of
asking, and read "completed" with the error as the step's answer. Now the card pauses the run under
every level, only the person answers it, and their approval runs exactly that call. A step whose
agent call ends in an error fails its run, with a plain reason, and leaves its chat settled.

The model is faked; the run, the Agent graph, the approval gate and the checkpointer are real.
Security tests: PR lane.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool

pytestmark = pytest.mark.subsystem

COMMAND = "mkdir wf-always-ask-test"
CALL = {"id": "call-1", "name": "run_command", "args": {"command": COMMAND}, "type": "tool_call"}
ANSWER = "Made the folder."
# A safety net against a hang if the code under test goes another way: never reached otherwise.
WAIT_SECONDS = 30


class Model(FakeMessagesListChatModel):
    """The model: its scripted replies in turn, or the provider's error when it has one."""

    error: str = ""

    def bind_tools(self, tools: Any, **kwargs: Any) -> Model:
        return self

    def _generate(self, messages: Any, stop: Any = None, run_manager: Any = None, **kwargs: Any) -> Any:
        if self.error:
            raise RuntimeError(self.error)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _shell(ran: list[str], *, ask: Any = None) -> StructuredTool:
    """run_command as the shell tool declares it: LangChain hands it the run's config."""

    def run_command(command: str, approval_reason: str = "", config: RunnableConfig = None) -> str:
        """Run a shell command."""
        if ask is not None:
            ask()
        ran.append(command)
        return f"$ {command}\n[Exit code: 0]"

    return StructuredTool.from_function(func=run_command, name="run_command", description="Run a shell command.")


@pytest.fixture
def workflow(tmp_path, monkeypatch, reload_for_data_dir):
    tasks, threads = reload_for_data_dir(tmp_path / "data", "row_bot.tasks", "row_bot.threads")
    import row_bot.agent as agent
    from row_bot import approval_review, notifications
    from row_bot.agent_budget import pre_model_budget_hook

    ran: list[str] = []
    reviewed: list[dict] = []
    model = Model(responses=[AIMessage(content="", tool_calls=[CALL]), AIMessage(content=ANSWER)])
    tool = {"shell": _shell(ran)}

    agent.clear_agent_cache()
    monkeypatch.setattr(agent, "checkpointer", threads.checkpointer)
    monkeypatch.setattr(agent, "get_current_model", lambda: "model:test")
    monkeypatch.setattr(agent, "_ensure_agent_mode_ready", lambda model_label: SimpleNamespace(
        provider_id="test", runtime_model="test", capability_source="test", confidence="high"))
    monkeypatch.setattr(agent, "_get_llm_for_reasoning_plan", lambda *args, **kwargs: model)
    monkeypatch.setattr(agent, "get_context_size", lambda *args, **kwargs: 200_000)
    monkeypatch.setattr(agent, "_build_runtime_skill_snapshot", lambda: ((), (), False, "test-skills"))
    monkeypatch.setattr(agent, "_collect_agent_tool_candidates", lambda *_args: (
        [{"tool": tool["shell"], "source": "core", "parent": "shell"}], [], set()))
    # The request is the conversation as it stands: preparing it (prompt, recall, compaction) isn't under test.
    monkeypatch.setattr(agent, "_pre_model_trim", lambda state: {
        "llm_input_messages": state["messages"], "execution_budget": pre_model_budget_hook(state)})
    monkeypatch.setattr(approval_review, "review", lambda payload, **_kwargs: reviewed.append(payload))
    monkeypatch.setattr(notifications, "notify", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks, "_push_approval_to_channels", lambda *args, **kwargs: None)
    monkeypatch.setattr(tasks, "_deliver_to_channels", lambda *args, **kwargs: ("not_configured", ""))

    def start(*, mode: str = "auto", always_ask: bool = True) -> SimpleNamespace:
        task_id = tasks.create_task("Always ask test", steps=[
            {"id": "step_1", "type": "prompt", "prompt": f"Use the shell to run exactly: {COMMAND}",
             "always_ask": always_ask}], safety_mode=mode, enabled=False)
        thread_id = threads.create_thread("Always ask test run", seed_default_skills=False)
        tasks.run_task_background(task_id, thread_id, ["shell"], notification=False)
        _settle(thread_id)
        [history] = tasks.get_run_history(task_id, limit=1)
        return SimpleNamespace(task_id=task_id, thread_id=thread_id, run_id=history["id"])

    def answer(run: SimpleNamespace, approved: bool) -> None:
        [approval] = _pending(tasks, run.run_id)
        assert tasks.respond_to_approval(approval["resume_token"], approved) is True
        _settle(run.thread_id)

    def run_row(run: SimpleNamespace) -> dict:
        [history] = tasks.get_run_history(run.task_id, limit=1)
        return history

    yield SimpleNamespace(tasks=tasks, threads=threads, model=model, tool=tool, ran=ran, reviewed=reviewed,
                          start=start, answer=answer, run_row=run_row)
    agent.clear_agent_cache()


def _settle(thread_id: str) -> None:
    """Wait for the run's worker (the run, or its resume) to finish."""
    from row_bot.runtime.executions import generation_registry

    for handle in generation_registry.active(thread_id):
        assert handle.producer_done.wait(WAIT_SECONDS), "the workflow run never finished"


def _pending(tasks, run_id: str) -> list[dict]:
    return [row for row in tasks.get_pending_approvals() if row["run_id"] == run_id]


def _unanswered_calls(threads, thread_id: str) -> list[str]:
    """The tool calls of the run's chat that have no result: each would read "Running a command"."""
    messages = threads.get_latest_checkpoint_messages(thread_id)
    answered = {message.tool_call_id for message in messages if isinstance(message, ToolMessage)}
    return [call["id"] for message in messages if isinstance(message, AIMessage)
            for call in message.tool_calls if call["id"] not in answered]


@pytest.mark.parametrize("mode", ["approve", "auto", "full"])
def test_an_always_ask_step_pauses_for_the_person_and_their_approval_runs_the_command(workflow, mode):
    run = workflow.start(mode=mode)

    assert workflow.run_row(run)["status"] == "paused"
    assert workflow.ran == []
    [approval] = _pending(workflow.tasks, run.run_id)
    payload = json.loads(approval["approval_payload_json"])
    assert payload["person_only"] is True and payload["binding"]
    assert payload["card"]["text"] == COMMAND  # The card says exactly what will run.
    assert approval["timeout_at"]  # A person-only card expires (24 hours unless the step says).

    workflow.answer(run, True)

    assert workflow.ran == [COMMAND]
    assert workflow.reviewed == []  # Only the person answered: no reviewer, and Hands-off didn't skip it.
    finished = workflow.run_row(run)
    assert (finished["status"], finished["steps_done"]) == ("completed", 1)
    messages = workflow.threads.get_latest_checkpoint_messages(run.thread_id)
    [result] = [message for message in messages if isinstance(message, ToolMessage)]
    assert str(result.content).startswith("Approval: asked; approved by you")
    assert messages[-1].content == ANSWER


def test_a_denied_always_ask_command_never_runs(workflow):
    run = workflow.start(mode="auto")

    workflow.answer(run, False)

    assert workflow.ran == []
    messages = workflow.threads.get_latest_checkpoint_messages(run.thread_id)
    [result] = [message for message in messages if isinstance(message, ToolMessage)]
    assert "did not run" in str(result.content)
    assert _unanswered_calls(workflow.threads, run.thread_id) == []


def _unsaveable_card() -> None:
    """A card holding something a checkpoint can't save, as the run's config was (B417's crash)."""
    from langgraph.types import interrupt

    interrupt({"tool": "run_command", "label": "Run shell command", "args": {"handle": object()}})


@pytest.mark.parametrize("failure", ["the provider fails", "its checkpoint can't be saved"])
def test_a_step_whose_agent_call_raises_fails_the_run_with_a_plain_reason(workflow, failure):
    if failure == "the provider fails":
        workflow.model.error = "Error code: 500 - internal server error"
    else:
        workflow.tool["shell"] = _shell(workflow.ran, ask=_unsaveable_card)

    run = workflow.start(mode="approve", always_ask=False)

    row = workflow.run_row(run)
    assert row["status"] == "failed"
    assert row["status_message"].startswith("Step 1 failed: ")
    assert "⚠" not in row["status_message"]
    assert workflow.ran == [] and _pending(workflow.tasks, run.run_id) == []
    # Its chat is settled: no tool call is left running.
    assert _unanswered_calls(workflow.threads, run.thread_id) == []
