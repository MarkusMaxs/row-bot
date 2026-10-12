"""Discord: each thread is its own conversation, with a rolling fallback (D6)."""

from __future__ import annotations

import asyncio
import itertools
import logging
from types import SimpleNamespace
from typing import Any

import pytest

discord = pytest.importorskip("discord")

from row_bot.channels.streaming import ChannelDeliveryResult  # noqa: E402


pytestmark = pytest.mark.subsystem

PERSON = 4242
_ids = itertools.count(9000)


class FakeChannel:
    def __init__(self, channel_id: int, channel_type, *, threads_allowed: bool = True, name: str = "") -> None:
        self.id = channel_id
        self.type = channel_type
        self.name = name
        self.sent: list[Any] = []
        self.guild = SimpleNamespace(me=object())
        self._threads_allowed = threads_allowed

    async def send(self, content: Any = None, **_kwargs: Any):
        self.sent.append(content)
        return SimpleNamespace(id=next(_ids))

    async def trigger_typing(self) -> None:
        return None

    def permissions_for(self, _member: Any):
        return SimpleNamespace(
            create_public_threads=self._threads_allowed,
            send_messages_in_threads=self._threads_allowed,
        )


class FakeAttachment:
    def __init__(self, filename: str, content_type: str, data: bytes) -> None:
        self.filename = filename
        self.content_type = content_type
        self._data = data

    async def read(self) -> bytes:
        return self._data


class FakeMessage:
    def __init__(self, channel: FakeChannel, content: str, *, mentions: list[Any] = (),
                 author_id: int = PERSON, bot: bool = False, attachments: list[Any] = (),
                 refuse_thread: bool = False) -> None:
        self.channel = channel
        self.content = content
        self.mentions = list(mentions)
        self.author = SimpleNamespace(id=author_id, bot=bot)
        self.attachments = list(attachments)
        self.created_threads: list[FakeChannel] = []
        self._refuse_thread = refuse_thread

    async def create_thread(self, *, name: str) -> FakeChannel:
        if self._refuse_thread:
            raise RuntimeError("50024: Cannot execute action on this channel type")
        thread = FakeChannel(next(_ids), discord.ChannelType.public_thread, name=name)
        self.created_threads.append(thread)
        return thread

    async def add_reaction(self, _emoji: str) -> None:
        return None

    async def remove_reaction(self, _emoji: str, _member: Any) -> None:
        return None


@pytest.fixture
def bot(tmp_path, reload_for_data_dir, monkeypatch):
    tasks, threads = reload_for_data_dir(tmp_path / "data", "row_bot.tasks", "row_bot.threads")
    from row_bot.channels import discord_channel

    turns: list[dict[str, Any]] = []

    async def streamed_turn(channel, text: str, config: dict):
        turns.append({"channel": channel, "text": text, "thread_id": config["configurable"]["thread_id"]})
        return "answer", None, [], [], ChannelDeliveryResult(
            delivered=True, streamed=True, finalized=True, fallback_sent=False,
            transport="discord:edit", final_text="answer",
        )

    monkeypatch.setattr(discord_channel, "_is_authorised", lambda user_id: user_id == PERSON)
    monkeypatch.setattr(discord_channel, "_stream_agent_turn_to_discord", streamed_turn)
    monkeypatch.setattr(discord_channel, "_pending_interrupts", {})
    monkeypatch.setattr(discord_channel, "_interrupt_by_message", {})
    monkeypatch.setattr(discord_channel, "_interrupt_by_channel", {})
    monkeypatch.setattr(discord_channel, "_pending_task_approvals", {})
    monkeypatch.setattr(discord_channel, "_task_approval_by_channel", {})
    discord_channel._thread_creation_failed.clear()
    discord_channel._thread_fallback_logged.clear()
    client = SimpleNamespace(user=SimpleNamespace(id=777))
    return SimpleNamespace(module=discord_channel, tasks=tasks, threads=threads, turns=turns, client=client)


def _send(bot, message: FakeMessage) -> None:
    asyncio.run(bot.module._on_discord_message(bot.client, message))


def _mention(bot, channel: FakeChannel, text: str, **kwargs: Any) -> FakeMessage:
    message = FakeMessage(channel, f"<@{bot.client.user.id}> {text}", mentions=[bot.client.user], **kwargs)
    _send(bot, message)
    return message


def test_a_mention_in_a_server_channel_opens_a_discord_thread_for_its_conversation(bot) -> None:
    channel = FakeChannel(100, discord.ChannelType.text)
    bot.threads._save_thread_meta("discord_100", "🎮 Discord conversation")  # the old per-channel thread

    first = _mention(bot, channel, "plan the release")
    (thread,) = first.created_threads
    _send(bot, FakeMessage(thread, "what about QA?"))  # a reply in the thread, no mention
    second = _mention(bot, channel, "different topic")

    plan, follow_up, other = bot.turns
    assert thread.name == "plan the release"
    assert plan["channel"] is thread and plan["thread_id"] == f"discord_{thread.id}"
    assert follow_up["thread_id"] == plan["thread_id"]
    assert other["thread_id"] == f"discord_{second.created_threads[0].id}" != plan["thread_id"]
    assert "discord_100" not in {turn["thread_id"] for turn in bot.turns}
    assert bot.tasks.get_thread_channel_ref(plan["thread_id"])["target"] == str(thread.id)


def test_people_and_bots_talking_in_a_thread_are_left_alone(bot) -> None:
    channel = FakeChannel(100, discord.ChannelType.text)
    first = _mention(bot, channel, "plan the release")
    (thread,) = first.created_threads

    _send(bot, FakeMessage(thread, "not for the bot", author_id=1))
    _send(bot, FakeMessage(thread, "a bot talking", bot=True))
    _send(bot, FakeMessage(FakeChannel(555, discord.ChannelType.public_thread), "unknown thread"))

    assert len(bot.turns) == 1
    assert thread.sent == []


def test_without_thread_permission_a_channel_keeps_one_rolling_conversation(bot, caplog) -> None:
    channel = FakeChannel(200, discord.ChannelType.text, threads_allowed=False)
    bot.threads._save_thread_meta("discord_200", "🎮 Discord conversation")

    with caplog.at_level(logging.INFO, logger="row_bot.discord"):
        before = _mention(bot, channel, "hello")
        _mention(bot, channel, "/new")
        _mention(bot, channel, "hello again")

    assert before.created_threads == []
    first, after = bot.turns
    assert first["channel"] is channel and first["thread_id"] == "discord_200"
    assert after["thread_id"].startswith("discord_200_")
    assert channel.sent == ["🆕 Started a new conversation. The earlier one stays in Row-Bot."]
    assert sum("keeps one rolling conversation" in record.message for record in caplog.records) == 1


def test_when_discord_refuses_a_thread_the_channel_falls_back_once(bot) -> None:
    channel = FakeChannel(300, discord.ChannelType.text)

    _mention(bot, channel, "hello", refuse_thread=True)
    second = _mention(bot, channel, "again")

    assert [turn["thread_id"] for turn in bot.turns] == ["discord_300", "discord_300"]
    assert second.created_threads == []  # not tried again until the bot restarts


def test_new_in_a_discord_thread_moves_only_that_thread(bot) -> None:
    channel = FakeChannel(100, discord.ChannelType.text)
    first = _mention(bot, channel, "topic a")
    (thread,) = first.created_threads

    _send(bot, FakeMessage(thread, "/new"))
    _send(bot, FakeMessage(thread, "topic a again"))

    before, after = bot.turns
    assert after["thread_id"].startswith(before["thread_id"] + "_")
    assert thread.sent[0].startswith("🆕 This thread now continues in a new conversation")


def test_discord_stuck_thread_recovery_moves_the_conversation_on(bot, monkeypatch) -> None:
    import row_bot.agent as agent

    healthy_turn = bot.module._stream_agent_turn_to_discord
    stuck: list[str] = []

    async def stuck_once(channel, text: str, config: dict):
        if not stuck:
            stuck.append(config["configurable"]["thread_id"])
            raise RuntimeError("must be followed by tool messages responding to each tool_call_id")
        return await healthy_turn(channel, text, config)

    def repair_fails(*_args: Any) -> None:
        raise RuntimeError("still stuck")

    monkeypatch.setattr(bot.module, "_stream_agent_turn_to_discord", stuck_once)
    monkeypatch.setattr(agent, "repair_orphaned_tool_calls", repair_fails)
    dm = FakeChannel(400, discord.ChannelType.private)

    _send(bot, FakeMessage(dm, "do it"))
    _send(bot, FakeMessage(dm, "again"))

    assert stuck == ["discord_400"]
    assert bot.turns[-1]["thread_id"].startswith("discord_400_")
    assert "started a fresh thread" in dm.sent[0]


def test_a_discord_dm_keeps_one_conversation_until_new(bot) -> None:
    dm = FakeChannel(400, discord.ChannelType.private)
    bot.threads._save_thread_meta("discord_400", "🎮 Discord conversation")

    _send(bot, FakeMessage(dm, "hi"))
    _send(bot, FakeMessage(dm, "/new"))
    _send(bot, FakeMessage(dm, "fresh"))

    before, after = bot.turns
    assert before["thread_id"] == "discord_400"
    assert after["thread_id"].startswith("discord_400_")
    assert dm.sent == ["🆕 Started a new conversation. The earlier one stays in Row-Bot."]


def test_attachments_in_one_discord_message_become_one_turn(bot, monkeypatch) -> None:
    from row_bot.channels import media

    analysed: list[bytes] = []

    def analyze(data: bytes, _question: str) -> str:
        analysed.append(data)
        return f"a picture of {data.decode()}"

    monkeypatch.setattr(media, "analyze_image", analyze)
    monkeypatch.setattr(media, "transcribe_audio", lambda data, _ext: f"spoken {data.decode()}")
    dm = FakeChannel(400, discord.ChannelType.private)

    _send(bot, FakeMessage(dm, "compare these", attachments=[
        FakeAttachment("a.png", "image/png", b"cat"),
        FakeAttachment("b.png", "image/png", b"dog"),
        FakeAttachment("note.ogg", "audio/ogg", b"memo"),
    ]))

    assert analysed == [b"cat", b"dog"]
    (turn,) = bot.turns
    assert turn["text"].startswith("compare these\n\n[Voice message transcription]: spoken memo")
    assert "a picture of cat" in turn["text"] and "a picture of dog" in turn["text"]
