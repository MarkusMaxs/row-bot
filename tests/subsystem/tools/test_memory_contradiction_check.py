"""WP C1: the memory tool's contradiction check is a background call on the turn's own model.

On local Ollama a check that loaded the default model mid-turn would swap the
model out (F13); the check also runs at the lowest effort with a short cap and
is counted as ``contradiction`` for the running conversation.
"""

from __future__ import annotations

import contextvars
import sqlite3
from contextlib import closing

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from tests.fixtures.memory_stack import fresh_memory_stack

pytestmark = pytest.mark.subsystem

TURN_MODEL = "model:ollama:qwen3:8b"


class _Checker(BaseChatModel):
    replies: list[str] = []
    calls: list[dict] = []
    num_ctx: int = 8192

    @property
    def _llm_type(self) -> str:
        return "contradiction-fixture"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append(dict(kwargs))
        message = AIMessage(content=self.replies.pop(0), usage_metadata={
            "input_tokens": 80, "output_tokens": 12, "total_tokens": 92})
        return ChatResult(generations=[ChatGeneration(message=message)])


@pytest.fixture
def memory(tmp_path, monkeypatch, reload_for_data_dir):
    stack = fresh_memory_stack(tmp_path, monkeypatch, reload_for_data_dir)
    from row_bot import models
    from row_bot.providers import background, usage_ledger

    checker = usage_ledger.attach_usage_ledger(_Checker(), provider_id="ollama", model_name="qwen3:8b")
    requested: list[str] = []

    def get_llm_for(ref, num_ctx=None, *, reasoning_plan=None):
        requested.append(ref)
        return checker

    monkeypatch.setattr(models, "get_llm_for", get_llm_for)
    monkeypatch.setattr(models, "get_current_model", lambda: "model:ollama:llama3.2:3b")
    monkeypatch.setattr(background, "resolve_reasoning_capabilities_for_ref", lambda _ref: None)
    return stack["memory_tool"], checker, requested, tmp_path / "row-bot-data" / "usage.db"


def _in_turn(fn):
    from row_bot import agent, models

    def run():
        models.set_active_model_override(TURN_MODEL)
        agent._current_thread_id_var.set("thread-live")
        agent._current_generation_id_var.set("gen-live")
        return fn()

    return contextvars.copy_context().run(run)


def test_a_contradiction_found_by_the_turns_model_is_reported(memory) -> None:
    memory_tool, checker, requested, ledger = memory
    memory_tool._save_memory("fact", "Home city", "User lives in London.")
    checker.replies = ['{"contradiction": true, "conflict": "London and Paris cannot both be home."}']

    reply = _in_turn(lambda: memory_tool._save_memory("fact", "Home city", "User lives in Paris."))

    assert "CONFLICT" in reply and "London and Paris cannot both be home." in reply
    # The turn's own local model, with its context allocation and a short reply: no model swap.
    assert requested == [TURN_MODEL]
    [call] = checker.calls
    assert call["options"] == {"num_ctx": 8192, "num_predict": 160}
    assert call["format"]["required"] == ["contradiction", "conflict"]
    with closing(sqlite3.connect(ledger)) as connection:
        rows = connection.execute("SELECT purpose, thread_id, generation_id FROM model_usage").fetchall()
    assert rows == [("contradiction", "thread-live", "gen-live")]


def test_compatible_facts_are_merged_from_a_schema_or_a_plain_no(memory) -> None:
    memory_tool, checker, _requested, _ledger = memory
    memory_tool._save_memory("fact", "Home city", "User lives in London.")
    checker.replies = ['{"contradiction": false, "conflict": ""}', "NO"]

    first = _in_turn(lambda: memory_tool._save_memory("fact", "Home city", "User works from home on Fridays."))
    second = _in_turn(lambda: memory_tool._save_memory("fact", "Home city", "User cycles to the office."))

    assert "CONFLICT" not in first and "CONFLICT" not in second
    assert len(checker.calls) == 2
