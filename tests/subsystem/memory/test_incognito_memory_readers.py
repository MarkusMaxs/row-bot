"""Incognito conversations are never read for memory (N11).

Background extraction skips an incognito chat; turning incognito off never
reaches back (the boundary, and the watermark moved to the end); Dream Cycle
and other chats' past-conversation search don't see it, while the chat can
still search itself. Memory privacy: these stay in the pull request lane.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

pytestmark = pytest.mark.subsystem

PROMPT_END = "\n\nRespond with ONLY a valid JSON array."
SECRET = "My blood test came back with a high cholesterol reading."


class _Model:
    """The default chat model as extraction sees it: records what each call read."""

    def __init__(self) -> None:
        self.read: list[str] = []
        self.on_call = None
        self.reply = '{"memories": []}'

    def bind(self, **_kwargs):
        return self

    def invoke(self, messages):
        prompt = str(messages[-1].content)
        self.read.append(prompt.split("CONVERSATION:\n", 1)[1].split(PROMPT_END, 1)[0])
        if self.on_call is not None:
            self.on_call()
        return AIMessage(content=self.reply)


@pytest.fixture
def stack(reload_for_data_dir, tmp_path, monkeypatch):
    threads, memory_extraction = reload_for_data_dir(
        tmp_path / "data", "row_bot.threads", "row_bot.memory_extraction")
    from row_bot import models
    from row_bot.providers import background

    model = _Model()
    monkeypatch.setattr(models, "get_current_model", lambda: "model:openai:gpt-5")
    monkeypatch.setattr(models, "get_llm_for", lambda _ref, num_ctx=None, *, reasoning_plan=None: model)
    monkeypatch.setattr(background, "resolve_reasoning_capabilities_for_ref", lambda _ref: None)
    monkeypatch.setattr(threads, "get_workflow_thread_ids", lambda: set())
    return threads, memory_extraction, model


def _chat(threads, thread_id: str, turns: list[tuple[str, str]], *, incognito: bool = False) -> None:
    threads.create_thread(thread_id, thread_id=thread_id, seed_default_skills=False, incognito=incognito)
    _say(threads, thread_id, turns)


def _say(threads, thread_id: str, turns: list[tuple[str, str]]) -> None:
    offset = len(threads.get_latest_checkpoint_messages(thread_id))
    messages = []
    for index, (person, answer) in enumerate(turns, start=offset):
        messages += [HumanMessage(id=f"{thread_id}-h{index}", content=person),
                     AIMessage(id=f"{thread_id}-a{index}", content=answer)]
    assert threads.append_checkpoint_messages(thread_id, messages)


# ── The flag and the boundary ────────────────────────────────────────────────


def test_the_helper_gives_the_flag_and_where_incognito_was_last_turned_off(stack) -> None:
    threads, _memory_extraction, _model = stack
    assert threads.incognito_state("never-made") == threads.IncognitoState(False, None)

    _chat(threads, "private", [(SECRET, "Thanks for telling me.")], incognito=True)
    assert threads.incognito_state("private") == threads.IncognitoState(True, None)

    revision = threads.get_latest_checkpoint_revision("private")
    off = threads.set_thread_incognito("private", False)

    assert off.incognito is False
    assert off.boundary is not None
    assert (off.boundary.message_id, off.boundary.message_count, off.boundary.checkpoint_id) == (
        "private-a0", 2, revision)
    assert off.boundary.turned_off_at
    assert threads.incognito_state("private") == off

    _say(threads, "private", [("Let's plan a picnic.", "Sounds fun.")])
    messages = threads.get_latest_checkpoint_messages("private")
    # Everything up to the boundary was said incognito; only what came after may be read.
    assert [message.id for message in messages[off.readable_from(messages):]] == ["private-h2", "private-a2"]

    # Turned on again: nothing may be read, and the last boundary stays for a reader to see.
    on = threads.set_thread_incognito("private", True)
    assert on.incognito is True and on.boundary == off.boundary
    assert on.readable_from(messages) == len(messages)


def test_a_boundary_whose_message_is_gone_keeps_what_it_covered_unread() -> None:
    from row_bot.threads import IncognitoBoundary

    boundary = IncognitoBoundary("edited-away", 2, "checkpoint", "2026-10-23T09:00:00")
    rewritten = [SimpleNamespace(id=name) for name in ("h0", "a0", "h1", "a1")]

    assert boundary.readable_from(rewritten) == 2
    assert boundary.readable_from(rewritten[:1]) == 1  # A shorter history: none of it is read.


def test_a_replaced_conversation_row_never_turns_incognito_off(stack) -> None:
    threads, _memory_extraction, _model = stack
    _chat(threads, "private", [(SECRET, "Noted.")], incognito=True)

    threads.create_thread("Renamed", thread_id="private", seed_default_skills=False)

    assert threads.incognito_state("private").incognito is True


# ── Background extraction ────────────────────────────────────────────────────


def test_extraction_skips_an_incognito_chat(stack) -> None:
    threads, memory_extraction, model = stack
    _chat(threads, "private", [(SECRET, "Thanks for telling me.")], incognito=True)
    _chat(threads, "everyday", [("I live in Leeds.", "Lovely city.")])

    memory_extraction.run_extraction()

    assert len(model.read) == 1
    assert "Leeds" in model.read[0]
    assert "cholesterol" not in "".join(model.read)


def test_turning_incognito_off_never_reads_what_was_said_before(stack) -> None:
    threads, memory_extraction, model = stack
    _chat(threads, "private", [(SECRET, "Thanks for telling me."), ("My doctor is Dr Patel.", "Noted.")],
          incognito=True)
    memory_extraction.run_extraction()
    assert model.read == []

    threads.set_thread_incognito("private", False)
    memory_extraction.run_extraction()
    assert model.read == []  # Nothing said since: the watermark is at the end of the chat.

    _say(threads, "private", [("I just adopted a cat called Miso.", "Congratulations!")])
    memory_extraction.run_extraction()

    [read] = model.read
    assert "Miso" in read
    # Not even as context before the new messages.
    assert "cholesterol" not in read and "Dr Patel" not in read and "Noted." not in read


def test_turning_incognito_on_stops_reads_at_once(stack) -> None:
    threads, memory_extraction, model = stack
    _chat(threads, "chat", [("I live in Leeds.", "Lovely city.")])
    memory_extraction.run_extraction()
    assert len(model.read) == 1

    threads.set_thread_incognito("chat", True)
    _say(threads, "chat", [(SECRET, "Thanks for telling me.")])
    memory_extraction.run_extraction()

    assert len(model.read) == 1


def test_a_chat_turned_incognito_while_it_is_read_saves_nothing(stack, monkeypatch) -> None:
    threads, memory_extraction, model = stack
    _chat(threads, "chat", [(SECRET, "Thanks for telling me.")])
    saved: list[object] = []
    monkeypatch.setattr(memory_extraction, "_dedup_and_save", lambda extracted, **_kwargs: saved.append(extracted) or 1)
    model.reply = ('{"memories": [{"category": "fact", "subject": "Cholesterol", '
                   '"content": "High cholesterol reading", "confidence": 0.95}]}')
    model.on_call = lambda: threads.set_thread_incognito("chat", True)

    memory_extraction.run_extraction()

    assert len(model.read) == 1
    assert saved == []


# ── Dream Cycle and past-conversation search ─────────────────────────────────


def test_dream_cycle_never_finds_an_incognito_chat_or_its_incognito_part(stack) -> None:
    threads, _memory_extraction, _model = stack
    from row_bot import dream_cycle

    _chat(threads, "private", [("Miso the cat hates the vet.", "Poor Miso.")], incognito=True)
    _chat(threads, "was-private", [("Miso is on a diet.", "Good luck, Miso.")], incognito=True)
    threads.set_thread_incognito("was-private", False)
    _say(threads, "was-private", [("Miso loves tuna.", "Of course.")])
    _chat(threads, "everyday", [("Miso sleeps all day.", "Cats do.")])

    excerpts = " ".join(dream_cycle._find_conversation_mentions("Miso"))

    assert "sleeps all day" in excerpts and "loves tuna" in excerpts
    assert "vet" not in excerpts and "diet" not in excerpts


def test_other_chats_search_never_sees_an_incognito_chat_but_it_can_search_itself(stack, monkeypatch) -> None:
    threads, _memory_extraction, _model = stack
    from row_bot import tool_outputs
    from row_bot.tools import conversation_search_tool as search

    _chat(threads, "private", [("Where is my passport?", "In the blue drawer.")], incognito=True)
    threads._save_thread_meta("private", "Passport worries")
    _chat(threads, "was-private", [("My passport expires in May.", "Renew it soon.")], incognito=True)
    threads.set_thread_incognito("was-private", False)
    _say(threads, "was-private", [("I renewed my passport.", "Great.")])
    _chat(threads, "everyday", [("Plan a trip.", "Where to?")])

    monkeypatch.setattr(tool_outputs, "current_thread_id", lambda: "everyday")
    found = search._search_conversations("passport")
    listed = search._list_conversations()

    assert "blue drawer" not in found and "expires in May" not in found
    assert "I renewed my passport." in found
    assert "Passport worries" not in listed

    monkeypatch.setattr(tool_outputs, "current_thread_id", lambda: "private")
    own = search._search_conversations("passport")

    assert "Where is my passport?" in own
    assert "Passport worries" in search._list_conversations()
