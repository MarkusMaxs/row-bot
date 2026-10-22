"""Row-Bot's questions in chat apps (N4).

Telegram shows a button per option (toggles plus Done when several may be
picked); Discord, Slack and WhatsApp number the options and read the reply.
Replying with words is the person's own answer, "skip" skips, and several
questions are asked one at a time. The turn goes on with the answer for the
question alone; an approval waiting in the same chat is never answered by it.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from row_bot import questions
from row_bot.channels import approval as helpers

pytestmark = pytest.mark.subsystem

_BETA = {"header": "Beta timing", "question": "When should the beta go out?",
         "options": [{"label": "Beta.1 now (Recommended)", "description": "Ship today."},
                     {"label": "After the fixes", "description": "About two days."}]}
_PAGES = {"header": "Pages", "question": "Which pages go in?", "multi_select": True,
          "options": [{"label": "Plans"}, {"label": "Ledger"}, {"label": "Docs"}]}


def _asked(*items: dict) -> dict:
    return {**questions.card(questions.normalize(list(items))), "__interrupt_id": "q-1"}


# ── Numbered replies (Discord, Slack, WhatsApp, SMS) ─────────────────────────


def test_a_question_reads_as_numbered_options_with_how_to_answer() -> None:
    text = helpers.interrupt_prompt([_asked(_BETA)], "Reply YES or NO.")

    assert text.splitlines() == [
        "❓ Row-Bot has a question: Beta timing",
        "When should the beta go out?",
        "1. Beta.1 now (recommended): Ship today.",
        "2. After the fixes: About two days.",
        "",
        "Reply with a number, or write your own answer. Reply skip to skip.",
    ]
    assert "YES or NO" not in text


@pytest.mark.parametrize(("reply", "expected"), [
    ("2", {"choices": [1], "other": ""}),
    (" 1 ", {"choices": [0], "other": ""}),
    ("only plans for now", {"choices": [], "other": "only plans for now"}),
])
def test_a_reply_answers_with_the_option_number_or_the_persons_own_words(reply, expected) -> None:
    pending = {"data": [_asked(_BETA)]}

    state, value = helpers.question_reply(pending, reply)

    assert state == "answered"
    assert value == questions.answer(_asked(_BETA), [expected])
    assert helpers.question_resume(pending["data"], value) == {"q-1": value}


@pytest.mark.parametrize("reply", ["3", "1 2", "0"])
def test_a_number_that_isnt_an_option_asks_again(reply) -> None:
    state, hint = helpers.question_reply({"data": [_asked(_BETA)]}, reply)

    assert state == "again"
    assert hint.startswith("Reply with one number from 1 to 2")


def test_skip_skips_the_whole_card() -> None:
    assert helpers.question_reply({"data": [_asked(_BETA, _PAGES)]}, "Skip") == ("skipped", questions.skipped())
    assert helpers.answered_text([_asked(_BETA)], questions.skipped()).startswith("Skipped.")


def test_several_questions_are_asked_one_at_a_time() -> None:
    pending = {"data": [_asked(_BETA, _PAGES)]}

    state, prompt = helpers.question_reply(pending, "1")
    assert state == "next"
    assert prompt.splitlines()[0] == "❓ Row-Bot has a question (2 of 2): Pages"
    assert "Reply with one or more numbers, like 1 3" in prompt

    state, value = helpers.question_reply(pending, "1 3")
    assert state == "answered"
    assert helpers.answered_text(pending["data"], value) == "You chose: Beta timing: Beta.1 now; Pages: Plans, Docs"


def test_a_resolved_request_reads_in_words_on_other_chats() -> None:
    assert [helpers.resolution_words(status) for status in ("approved", "denied", "answered", "skipped")] == [
        "✅ Approved", "❌ Denied", "✅ Answered", "⏭️ Skipped"]


# ── Discord ──────────────────────────────────────────────────────────────────

discord = pytest.importorskip("discord")
PERSON = 4242


@pytest.fixture
def discord_bot(tmp_path, reload_for_data_dir, monkeypatch):
    reload_for_data_dir(tmp_path / "data", "row_bot.tasks", "row_bot.threads")
    from row_bot.channels import discord_channel
    from row_bot.channels.streaming import ChannelDeliveryResult

    delivered = ChannelDeliveryResult(delivered=True, streamed=True, finalized=True, fallback_sent=False,
                                      transport="discord:edit", final_text="answer")
    resumed: list[dict] = []
    asked = {"data": [_asked(_BETA)]}

    async def streamed_turn(channel, text: str, config: dict):
        return "", asked["data"], [], [], delivered

    async def resumed_turn(channel, config, approved, *, interrupt_ids=None, resume_values=None):
        resumed.append(resume_values)
        return "Going with that.", None, [], [], delivered

    from row_bot.channels import base

    monkeypatch.setattr(base, "_channel_activity", {})  # this chat's messages stay this test's own
    monkeypatch.setattr(discord_channel, "_is_authorised", lambda user_id: user_id == PERSON)
    monkeypatch.setattr(discord_channel, "_stream_agent_turn_to_discord", streamed_turn)
    monkeypatch.setattr(discord_channel, "_stream_agent_resume_to_discord", resumed_turn)
    for name in ("_pending_interrupts", "_interrupt_by_message", "_interrupt_by_channel",
                 "_pending_task_approvals", "_task_approval_by_channel"):
        monkeypatch.setattr(discord_channel, name, {})

    class Channel:
        id = 501
        type = discord.ChannelType.private

        def __init__(self) -> None:
            self.sent: list[tuple[Any, dict]] = []

        async def send(self, content: Any = None, **kwargs: Any):
            self.sent.append((content, kwargs))
            return SimpleNamespace(id=len(self.sent) + 9000)

        async def trigger_typing(self) -> None:
            return None

    channel = Channel()

    def say(text: str) -> None:
        message = SimpleNamespace(channel=channel, content=text, mentions=[], attachments=[],
                                  author=SimpleNamespace(id=PERSON, bot=False))

        async def noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        message.add_reaction = message.remove_reaction = noop
        asyncio.run(discord_channel._on_discord_message(SimpleNamespace(user=SimpleNamespace(id=777)), message))

    return SimpleNamespace(module=discord_channel, channel=channel, say=say, resumed=resumed)


def test_discord_numbers_the_options_and_goes_on_with_the_reply(discord_bot) -> None:
    discord_bot.say("plan the beta")
    question, kwargs = discord_bot.channel.sent[-1]
    assert "1. Beta.1 now (recommended)" in question
    assert "view" not in kwargs  # answered by reply, never with Approve / Deny

    discord_bot.say("2")

    assert discord_bot.resumed == [{"q-1": questions.answer(_asked(_BETA), [{"choices": [1], "other": ""}])}]
    assert "You chose: After the fixes" in [content for content, _ in discord_bot.channel.sent]


def test_discord_answers_a_workflows_question_by_reply(discord_bot, monkeypatch) -> None:
    from row_bot import tasks

    answered: list[tuple] = []
    monkeypatch.setattr(tasks, "respond_to_approval",
                        lambda token, approved, **kwargs: answered.append((token, approved, kwargs["answer"])) or True)
    discord_bot.module._pending_task_approvals[77] = {"resume_token": "token-1", "task_name": "Daily plan",
                                                     "data": _asked(_BETA)}
    discord_bot.module._task_approval_by_channel[discord_bot.channel.id] = 77

    discord_bot.say("ship the docs too")

    assert answered == [("token-1", True, questions.answer(_asked(_BETA), [{"choices": [], "other": "ship the docs too"}]))]
    assert discord_bot.resumed == []


# ── Slack ────────────────────────────────────────────────────────────────────


def test_slack_asks_in_the_thread_and_reads_the_numbered_reply(tmp_path, reload_for_data_dir, monkeypatch) -> None:
    reload_for_data_dir(tmp_path / "data", "row_bot.tasks", "row_bot.threads")
    from row_bot.channels import slack
    from row_bot.channels.streaming import ChannelDeliveryResult

    delivered = ChannelDeliveryResult(delivered=True, streamed=True, finalized=True, fallback_sent=False,
                                      transport="slack:edit", final_text="answer")
    resumed: list[dict] = []
    said: list[str] = []
    posts: list[dict] = []

    async def streamed_turn(_client, channel_id, text, config, *, thread_ts=None):
        return "", [_asked(_BETA, _PAGES)], [], [], delivered

    async def resumed_turn(_client, channel_id, config, approved, *, interrupt_ids=None, resume_values=None,
                           thread_ts=None):
        resumed.append(resume_values)
        return "Going with that.", None, [], [], delivered

    class Client:
        async def chat_postMessage(self, **kwargs: Any) -> dict:
            posts.append(kwargs)
            return {"channel": kwargs["channel"], "ts": "9.1"}

        async def reactions_add(self, **_kwargs: Any) -> None:
            return None

        async def reactions_remove(self, **_kwargs: Any) -> None:
            return None

    async def say(text: str, **_kwargs: Any) -> None:
        said.append(text)

    from row_bot.channels import base

    monkeypatch.setattr(base, "_channel_activity", {})  # this chat's messages stay this test's own
    monkeypatch.setattr(slack, "_bot_user_id", "UBOT")
    monkeypatch.setattr(slack, "_is_authorised", lambda user_id: user_id == "UPERSON")
    monkeypatch.setattr(slack, "_stream_agent_turn_to_slack", streamed_turn)
    monkeypatch.setattr(slack, "_stream_agent_resume_to_slack", resumed_turn)
    for name in ("_pending_interrupts", "_interrupt_by_message", "_interrupt_by_channel",
                 "_pending_task_approvals", "_approval_by_channel"):
        monkeypatch.setattr(slack, name, {})

    def message(text: str, ts: str) -> None:
        event = {"user": "UPERSON", "channel": "D1", "channel_type": "im", "text": text, "ts": ts}
        asyncio.run(slack._route_message_event(event, say, Client()))

    message("plan the beta", "100.1")
    assert "blocks" not in posts[-1] and "1. Beta.1 now (recommended)" in posts[-1]["text"]
    message("1", "100.2")
    assert "(2 of 2): Pages" in said[-1]
    message("9", "100.3")
    assert said[-1].startswith("Reply with numbers from 1 to 3")
    message("2 3", "100.4")

    expected = questions.answer(_asked(_BETA, _PAGES), [{"choices": [0], "other": ""}, {"choices": [1, 2], "other": ""}])
    assert resumed == [{"q-1": expected}]


# ── Telegram ─────────────────────────────────────────────────────────────────


def test_telegram_shows_a_button_per_option_and_toggles_plus_done_for_several() -> None:
    from row_bot.channels import telegram

    pending = {"data": [_asked(_BETA, _PAGES)]}
    text, keyboard = telegram._start_question(pending)
    token = pending["question_progress"]["token"]

    assert "<b>Row-Bot has a question</b> (1 of 2) · Beta timing" in text
    assert [[(button.text, button.callback_data) for button in row] for row in keyboard.inline_keyboard] == [
        [("Beta.1 now (recommended)", f"q:{token}:0:0")],
        [("After the fixes", f"q:{token}:0:1")],
        [("Skip", f"q:{token}:0:s")],
    ]
    pending["question_progress"].update(step=1, picked=[2])
    rows = telegram._question_keyboard(questions.question_item(pending["data"]), pending["question_progress"])
    assert [[button.text for button in row] for row in rows.inline_keyboard] == [
        ["☐ Plans"], ["☐ Ledger"], ["☑ Docs"], ["Done", "Skip"]]


class _Query:
    def __init__(self, data: str, message_id: int) -> None:
        self.data = data
        self.message = SimpleNamespace(message_id=message_id, message_thread_id=None)
        self.edits: list[str] = []
        self.markups: list[Any] = []

    async def answer(self) -> None:
        return None

    async def edit_message_text(self, text: str, **_kwargs: Any) -> None:
        self.edits.append(text)

    async def edit_message_reply_markup(self, reply_markup: Any = None) -> None:
        self.markups.append(reply_markup)


def _tap(telegram, query: _Query) -> None:
    async def send(*_args: Any, **_kwargs: Any) -> None:
        return None

    update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=55, send_message=send),
                             effective_user=SimpleNamespace(id=1))
    asyncio.run(telegram._handle_callback(update, SimpleNamespace(chat_data={})))


@pytest.fixture
def telegram_chat(monkeypatch):
    from row_bot.channels import telegram

    continued: list[tuple] = []

    async def continue_after(update, context, chat_id, pending, approved, message, *, resume_values=None):
        continued.append((approved, resume_values))

    monkeypatch.setattr(telegram, "_is_authorised", lambda update: True)
    monkeypatch.setattr(telegram, "_continue_after_interrupt", continue_after)
    monkeypatch.setattr(telegram, "_pending_interrupts", {})
    monkeypatch.setattr(telegram, "_pending_task_approvals", {})
    return telegram, continued


def test_telegram_buttons_answer_each_question_and_the_turn_goes_on(telegram_chat) -> None:
    telegram, continued = telegram_chat
    pending = {"data": [_asked(_BETA, _PAGES)], "config": {}}
    telegram._start_question(pending)
    telegram._pending_interrupts[55] = pending
    token = pending["question_progress"]["token"]

    _tap(telegram, _Query(f"q:{token}:0:1", 10))      # After the fixes
    _tap(telegram, _Query(f"q:{token}:0:0", 10))      # a stale tap on the first question's buttons
    _tap(telegram, _Query(f"q:{token}:1:0", 10))      # toggle Plans
    _tap(telegram, _Query(f"q:{token}:1:2", 10))      # toggle Docs
    _tap(telegram, _Query(f"q:{token}:1:0", 10))      # Plans off again
    done = _Query(f"q:{token}:1:d", 10)
    _tap(telegram, done)

    expected = questions.answer(_asked(_BETA, _PAGES), [{"choices": [1], "other": ""}, {"choices": [2], "other": ""}])
    assert continued == [(True, {"q-1": expected})]
    assert done.edits == ["You chose: Beta timing: After the fixes; Pages: Docs"]
    assert 55 not in telegram._pending_interrupts


def test_telegram_approve_and_deny_never_answer_a_question(telegram_chat) -> None:
    telegram, continued = telegram_chat
    pending = {"data": [_asked(_BETA)], "config": {}}
    telegram._start_question(pending)
    telegram._pending_interrupts[55] = pending

    _tap(telegram, _Query("interrupt_approve", 10))

    assert continued == []
    assert telegram._pending_interrupts[55] is pending


def test_telegram_skips_a_workflows_question_from_its_buttons(telegram_chat, monkeypatch) -> None:
    from row_bot import tasks

    telegram, _continued = telegram_chat
    responses: list[tuple] = []
    monkeypatch.setattr(tasks, "respond_to_approval",
                        lambda token, approved, **kwargs: responses.append((token, approved, kwargs["answer"])) or True)
    pending = {"resume_token": "token-1", "task_name": "Daily plan", "data": _asked(_BETA)}
    telegram._start_question(pending)
    telegram._pending_task_approvals[20] = pending

    _tap(telegram, _Query(f"q:{pending['question_progress']['token']}:0:s", 20))

    assert responses == [("token-1", False, questions.skipped())]
    assert 20 not in telegram._pending_task_approvals
