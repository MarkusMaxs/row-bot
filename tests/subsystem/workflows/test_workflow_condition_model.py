"""WP C1 (F51): a workflow's yes/no condition is a background call on the workflow's model.

The task's model override comes first, then the workflow thread's model; the
call runs at the lowest reasoning effort with a few output tokens, asks for a
schema answer where the provider takes one, and still reads a plain "yes".
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from tests.subsystem.workflows.test_workflow_profile_runtime import (
    _fresh_modules, _install_fake_agent, _run_workflow_synchronously,
)

pytestmark = pytest.mark.subsystem

QUESTION = "Is any item below its reorder level?"
STEPS = [
    {"type": "prompt", "prompt": "Check stock levels"},
    {"type": "condition", "condition": f"llm:{QUESTION}", "if_true": "prompt_2", "if_false": "notify_1"},
    {"type": "prompt", "prompt": "Draft purchase orders"},
    {"type": "notify", "message": "Morning report ready"},
]


class _ConditionModel(BaseChatModel):
    reply: str = '{"answer": "yes"}'
    calls: list[dict] = []

    @property
    def _llm_type(self) -> str:
        return "condition-fixture"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append({"prompt": messages[-1].content, **kwargs})
        message = AIMessage(content=self.reply, usage_metadata={"input_tokens": 50, "output_tokens": 4,
                                                                 "total_tokens": 54})
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.fixture
def workflow(tmp_path, monkeypatch, reload_for_data_dir):
    tasks, threads, _profiles, _runs, _skills = _fresh_modules(tmp_path, reload_for_data_dir)
    calls: list[dict] = []
    _install_fake_agent(monkeypatch, calls)
    _run_workflow_synchronously(monkeypatch, tasks)
    monkeypatch.setattr(tasks, "_deliver_to_channels", lambda *_, **__: ("", ""))
    from row_bot import models, notifications
    from row_bot.providers import background, usage_ledger

    monkeypatch.setattr(notifications, "notify", lambda **_kwargs: None)
    model = usage_ledger.attach_usage_ledger(_ConditionModel(), provider_id="fixture", model_name="fixture")
    requested: list[str] = []

    def get_llm_for(ref, num_ctx=None, *, reasoning_plan=None):
        requested.append(ref)
        return model

    monkeypatch.setattr(models, "get_llm_for", get_llm_for)
    monkeypatch.setattr(models, "get_current_model", lambda: "model:openai:gpt-5")
    monkeypatch.setattr(background, "resolve_reasoning_capabilities_for_ref", lambda _ref: None)
    return tasks, threads, calls, model, requested


def test_a_condition_uses_the_tasks_model_and_follows_a_schema_yes(workflow, tmp_path):
    import sqlite3
    from contextlib import closing

    tasks, threads, agent_calls, model, requested = workflow
    task_id = tasks.create_task("Stock check", steps=STEPS, channels=[], apply_default_skills=False,
                                model_override="model:openai:gpt-4o-mini")
    thread_id = threads.create_thread("Run", thread_id="stock-check-run", seed_default_skills=False)

    tasks.run_task_background(task_id, thread_id, [], notification=False)

    assert requested == ["model:openai:gpt-4o-mini"]
    [call] = model.calls
    assert QUESTION in call["prompt"]
    assert call["max_tokens"] <= 64 + 4096
    assert call["response_format"]["json_schema"]["schema"]["properties"]["answer"]["enum"] == ["yes", "no"]
    assert "Draft purchase orders" in [entry["prompt"] for entry in agent_calls]
    with closing(sqlite3.connect(tmp_path / "data" / "usage.db")) as connection:
        rows = connection.execute("SELECT purpose, thread_id FROM model_usage").fetchall()
    assert rows == [("condition", thread_id)]


def test_without_a_task_model_the_workflow_threads_model_answers_in_plain_text(workflow):
    tasks, threads, agent_calls, model, requested = workflow
    model.reply = "No, every item is above its reorder level."
    task_id = tasks.create_task("Stock check", steps=STEPS, channels=[], apply_default_skills=False)
    thread_id = threads.create_thread("Run", thread_id="stock-check-thread-model", seed_default_skills=False)
    threads._set_thread_model_override(thread_id, "model:google:gemini-3-flash-preview")

    tasks.run_task_background(task_id, thread_id, [], notification=False)

    assert requested == ["model:google:gemini-3-flash-preview"]
    assert "Draft purchase orders" not in [entry["prompt"] for entry in agent_calls]
    run = tasks.get_run_history(task_id, limit=1)[0]
    assert run["status"] == "completed"
