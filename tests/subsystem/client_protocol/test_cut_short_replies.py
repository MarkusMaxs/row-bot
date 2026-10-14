"""A reply the provider cut off at its output token limit is marked in the transcript (F17)."""
# ruff: noqa: F811 -- reused pytest fixture.
from __future__ import annotations

import pytest

from tests.subsystem.client_protocol.test_protocol_application import _client, service  # noqa: F401
from tests.subsystem.client_protocol.test_protocol_security import bootstrap

pytestmark = pytest.mark.subsystem


def test_a_reply_cut_off_at_the_output_limit_is_marked_in_the_transcript(service):
    from langchain_core.messages import AIMessage, HumanMessage
    from row_bot import threads

    # Each provider's own shape, saved and read back through the real checkpoint.
    replies = {
        "anthropic": ({"stop_reason": "max_tokens", "model_name": "claude-fixture"}, True),
        "responses": ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}}, True),
        "gemini": ({"finish_reason": "MAX_TOKENS", "safety_ratings": []}, True),
        "ollama": ({"done_reason": "length", "model": "fixture"}, True),
        "finished": ({"stop_reason": "end_turn"}, False),
        "filtered": ({"status": "incomplete", "incomplete_details": None}, False),
        "no-metadata": ({}, False),
    }
    conversation = threads.create_thread("Long answers", seed_default_skills=False)
    messages = []
    for name, (metadata, _cut_short) in replies.items():
        messages += [HumanMessage(id=f"ask-{name}", content=f"Write it all ({name})"),
                     AIMessage(id=name, content=f"The first half ({name})", response_metadata=metadata)]
    assert threads.append_checkpoint_messages(conversation, messages)

    with _client(service) as client:
        _, headers = bootstrap(client)
        page = client.get(f"/api/v1/conversations/{conversation}/transcript", headers=headers)

    assert page.status_code == 200, page.text
    marked = {row["message_id"]: row.get("cut_short", False) for row in page.json()["rows"] if row["role"] == "assistant"}
    assert marked == {name: cut_short for name, (_metadata, cut_short) in replies.items()}
    # The reply itself stays the model's own text.
    [anthropic] = [row for row in page.json()["rows"] if row["message_id"] == "anthropic"]
    assert "cut short" not in str(anthropic["blocks"]).lower()
