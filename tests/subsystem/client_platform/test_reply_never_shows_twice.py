"""A reply never shows twice while its turn settles.

The streamed answer is a live row until Row-Bot binds it to the saved message. Binding read the saved
messages in (now holding the answer) and then retired the live row, as two steps: a page reading the
conversation between them got the answer twice, once saved and once live. Found in the browser smoke
(15 Oct, "what you send leaves the composer at once"): an instant reply showed twice for a moment.
"""
from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage

from tests.contracts.client_platform.test_headless_lifecycle import platform, submit  # noqa: F401
from tests.helpers.client_platform_fakes import CheckpointCommit, ScriptedAgentStream, fixture_id

pytestmark = pytest.mark.subsystem


def test_every_read_of_a_settling_turn_shows_the_reply_once(platform, monkeypatch):
    reads: list[list[str]] = []
    projection = platform.projection
    notify = projection._notify

    def read_when_pages_are_told(conversation_id, revision):
        # Each change wakes the pages following the conversation; they read it then.
        if conversation_id == "conversation-a":
            reads.append([str(row.get("message_id") or row["id"]) + ":" + "".join(
                block.get("text", "") for block in row.get("blocks", []))
                for row in projection.snapshot(conversation_id)["rows"] if row["role"] == "assistant"])
        return notify(conversation_id, revision)

    monkeypatch.setattr(projection, "_notify", read_when_pages_are_told)
    native_id = fixture_id("instant-native")
    fake = ScriptedAgentStream((("token", "Here it is, straight away."),
                               CheckpointCommit((AIMessage(content="Here it is, straight away.", id=native_id),),
                                                native_id),
                               ("done", "Here it is, straight away.")))
    handle = platform.registry.get(submit(platform, fake, "instant")["execution_id"])
    assert handle.producer_done.wait(10)

    replies = [[row for row in read if row.endswith(":Here it is, straight away.")] for read in reads]
    assert any(replies), "the reply was never read"
    assert all(len(found) <= 1 for found in replies), replies
    final = platform.snapshot("conversation-a")["rows"]
    assert [row["id"] for row in final if row["role"] == "assistant"] == ["assistant:checkpoint:" + native_id]
