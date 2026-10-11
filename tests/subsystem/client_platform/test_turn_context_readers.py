"""Row-Bot's turn context is for the model only: no reader of the conversation shows or reuses it.

The Agent keeps one ``<row_bot_turn_context>`` message per turn in the checkpoint, right after the
person's message (``row_bot.turn_context``). It is a HumanMessage, so every reader of history must
skip it: the transcript and its history pages, search, exports, memory extraction, the conversation
search tool, a child Agent's parent context, Chat Only, and the legacy projection.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from tests.contracts.client_platform.test_headless_lifecycle import platform  # noqa: F401

pytestmark = pytest.mark.subsystem
SENTINEL = "zebracontext"


def _context(turn_id: str = "turn-1"):
    from row_bot import turn_context

    values = turn_context.TurnContextValues(
        now=datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc),
        fields=(turn_context.ContextField("state", f"Current state:\n- {SENTINEL} model"),),
        memories=({"id": "mem_z", "subject": "Notes", "description": f"{SENTINEL} memory"},),
        recall_block=lambda memories: "Relevant long-term memory:\n" + "\n".join(
            f"- {memory['description']}" for memory in memories),
    )
    return turn_context.build_message(turn_id=turn_id, values=values, baseline=turn_context.Baseline(),
                                      summary_marker="")


def _seed() -> list:
    from row_bot import threads

    messages = [HumanMessage(id="h1", content="Which notes did I keep?"), _context(),
                AIMessage(id="a1", content="They are in notes.txt.")]
    assert threads.append_checkpoint_messages("conversation-a", messages)
    return messages


def test_no_reader_of_the_conversation_shows_the_turn_context(platform):
    from row_bot import agent, agent_context, memory_extraction, threads
    from row_bot.application.conversation_actions import _export_markdown
    from row_bot.application.conversation_search import history_window, search
    from row_bot.message_projection import langchain_messages_to_ui_messages
    from row_bot.tools import conversation_search_tool

    seeded = _seed()
    assert SENTINEL in str(seeded[1].content)  # the model's copy says it

    rows = platform.transcript("conversation-a")["rows"]
    assert [row["role"] for row in rows] == ["user", "assistant"]
    assert SENTINEL not in str(rows)
    assert SENTINEL not in str(history_window(platform, "conversation-a"))

    hits = [item for item in search(platform, SENTINEL)["items"] if item.get("message_id")]
    assert hits == []
    assert [item["message_id"] for item in search(platform, "notes.txt")["items"] if item.get("message_id")] == ["a1"]

    exported = _export_markdown("conversation-a", "Notes", "", validate=lambda: None).decode("utf-8")
    assert "Which notes did I keep?" in exported and SENTINEL not in exported
    assert exported.count("## You") == 1

    extracted = memory_extraction._get_thread_messages("conversation-a")
    assert [item["role"] for item in extracted] == ["user", "assistant"]
    assert SENTINEL not in memory_extraction._format_conversation(extracted)

    assert SENTINEL not in str(conversation_search_tool._get_thread_messages("conversation-a"))

    parent = agent_context.load_parent_context("conversation-a")
    assert parent["message_count"] == 2
    assert SENTINEL not in parent["full"] and "User: Which notes did I keep?" in parent["full"]

    raw = threads.get_latest_checkpoint_messages("conversation-a")
    assert SENTINEL not in str(langchain_messages_to_ui_messages(raw))
    chat_only = agent._collect_chat_only_preparation_inputs("conversation-a", "And the old ones?")
    assert SENTINEL not in str([message.content for message in chat_only.complete_messages])
    assert agent._user_led_groups(tuple(raw)) == [(0, 3)]  # the context belongs to its message's group


def test_removing_a_provisional_answer_keeps_the_turn_context_with_its_message(platform):
    from row_bot import threads, turn_context

    _seed()
    assert threads.remove_latest_checkpoint_ai_message("conversation-a", "They are in notes.txt.")
    remaining = threads.get_latest_checkpoint_messages("conversation-a")
    assert [message.id for message in remaining] == ["h1", f"{turn_context.MESSAGE_ID_PREFIX}turn-1"]
