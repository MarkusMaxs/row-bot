"""Slack: each thread in a channel is its own conversation; a DM rolls with /new (D6)."""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any

import pytest

from row_bot.channels.streaming import ChannelDeliveryResult


pytestmark = pytest.mark.subsystem

BOT = "UBOT"
PERSON = "UPERSON"


class FakeSlackClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []
        self.uploads: list[dict[str, Any]] = []

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        return {"channel": kwargs.get("channel"), "ts": f"9{len(self.posts)}.000"}

    async def files_upload_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.uploads.append(kwargs)
        return {"ok": True}

    async def reactions_add(self, **_kwargs: Any) -> None:
        return None

    async def reactions_remove(self, **_kwargs: Any) -> None:
        return None

    async def chat_update(self, **_kwargs: Any) -> None:
        return None


class Recorder:
    def __init__(self) -> None:
        self.said: list[tuple[str, dict[str, Any]]] = []
        self.turns: list[dict[str, Any]] = []

    async def say(self, text: str, **kwargs: Any) -> None:
        self.said.append((text, kwargs))


@pytest.fixture
def slack(tmp_path, reload_for_data_dir, monkeypatch):
    tasks, threads = reload_for_data_dir(tmp_path / "data", "row_bot.tasks", "row_bot.threads")
    from row_bot.channels import slack

    recorder = Recorder()
    client = FakeSlackClient()

    async def streamed_turn(_client, channel_id: str, text: str, config: dict, *, thread_ts=None):
        recorder.turns.append({
            "channel": channel_id,
            "text": text,
            "thread_id": config["configurable"]["thread_id"],
            "thread_ts": thread_ts,
        })
        return "answer", None, [], [], ChannelDeliveryResult(
            delivered=True, streamed=True, finalized=True, fallback_sent=False,
            transport="slack:edit", final_text="answer",
        )

    monkeypatch.setattr(slack, "_bot_user_id", BOT)
    monkeypatch.setattr(slack, "_is_authorised", lambda user_id: user_id == PERSON)
    monkeypatch.setattr(slack, "_stream_agent_turn_to_slack", streamed_turn)
    monkeypatch.setattr(slack, "_pending_interrupts", {})
    monkeypatch.setattr(slack, "_interrupt_by_message", {})
    monkeypatch.setattr(slack, "_interrupt_by_channel", {})
    monkeypatch.setattr(slack, "_pending_task_approvals", {})
    monkeypatch.setattr(slack, "_approval_by_channel", {})
    return SimpleNamespace(module=slack, tasks=tasks, threads=threads, recorder=recorder, client=client)


def _mention(s, text: str, ts: str, thread_ts: str | None = None, channel: str = "C1") -> None:
    event = {"user": PERSON, "channel": channel, "text": f"<@{BOT}> {text}", "ts": ts}
    if thread_ts:
        event["thread_ts"] = thread_ts
    asyncio.run(s.module._handle_mention(event, s.recorder.say, s.client))


def _message(s, text: str, ts: str, *, channel: str, channel_type: str,
             thread_ts: str | None = None, user: str = PERSON) -> None:
    event = {"user": user, "channel": channel, "channel_type": channel_type, "text": text, "ts": ts}
    if thread_ts:
        event["thread_ts"] = thread_ts
    asyncio.run(s.module._route_message_event(event, s.recorder.say, s.client))


def _name(threads, thread_id: str) -> str:
    with sqlite3.connect(threads.DB_PATH) as conn:
        row = conn.execute("SELECT name FROM thread_meta WHERE thread_id = ?", (thread_id,)).fetchone()
    return str(row[0]) if row else ""


def test_each_slack_thread_in_a_channel_is_its_own_conversation(slack) -> None:
    slack.threads._save_thread_meta("slack_C1", "💬 Slack – C1")  # the old per-channel thread

    _mention(slack, "what's the plan for the launch", "100.1")
    _mention(slack, "and the budget?", "100.2", thread_ts="100.1")
    _mention(slack, "something else entirely", "200.1")

    first, follow_up, other = slack.recorder.turns
    assert first["thread_id"] == "slack_C1_100_1" and first["thread_ts"] == "100.1"
    assert follow_up["thread_id"] == first["thread_id"] and follow_up["thread_ts"] == "100.1"
    assert other["thread_id"] == "slack_C1_200_1" and other["thread_ts"] == "200.1"
    assert "slack_C1" not in {turn["thread_id"] for turn in slack.recorder.turns}
    assert slack.threads._thread_exists("slack_C1")
    assert slack.tasks.get_thread_channel_ref("slack_C1_100_1")["target"] == "C1:100.1"
    assert _name(slack.threads, "slack_C1_100_1") == "💬 Slack – what's the plan for the launch"


def test_a_plain_reply_continues_only_a_thread_row_bot_is_part_of(slack) -> None:
    _mention(slack, "start here", "100.1")

    _message(slack, "and then?", "100.3", channel="C1", channel_type="channel", thread_ts="100.1")
    _message(slack, "unrelated thread", "300.2", channel="C1", channel_type="channel", thread_ts="300.1")
    _message(slack, "top of the channel", "400.1", channel="C1", channel_type="channel")
    _message(slack, f"<@{BOT}> mentioned", "100.4", channel="C1", channel_type="channel", thread_ts="100.1")
    _message(slack, "someone else", "100.5", channel="C1", channel_type="channel",
             thread_ts="100.1", user="USTRANGER")

    assert [turn["text"] for turn in slack.recorder.turns] == ["start here", "and then?"]
    assert slack.recorder.turns[1]["thread_id"] == "slack_C1_100_1"
    assert slack.recorder.said == []


def test_a_dm_keeps_one_conversation_until_new(slack) -> None:
    slack.threads._save_thread_meta("slack_D1", "💬 Slack – D1")  # from before this change

    _message(slack, "hi", "1.1", channel="D1", channel_type="im")
    _message(slack, "reply in a slack thread", "1.2", channel="D1", channel_type="im", thread_ts="1.1")
    _message(slack, "/new", "2.1", channel="D1", channel_type="im")
    _message(slack, "fresh start", "3.1", channel="D1", channel_type="im")

    before, threaded, after = slack.recorder.turns
    assert before["thread_id"] == threaded["thread_id"] == "slack_D1"
    assert after["thread_id"].startswith("slack_D1_") and after["thread_id"] != "slack_D1"
    assert slack.recorder.said == [
        ("🆕 Started a new conversation. The earlier one stays in Row-Bot.", {"channel": "D1"}),
    ]
    assert slack.threads._thread_exists("slack_D1")


def test_new_in_a_slack_thread_moves_only_that_thread(slack) -> None:
    _mention(slack, "thread a", "100.1")
    _mention(slack, "thread b", "200.1")
    _mention(slack, "/new", "100.2", thread_ts="100.1")
    _mention(slack, "thread a again", "100.3", thread_ts="100.1")
    _mention(slack, "thread b again", "200.2", thread_ts="200.1")

    a, b, a_again, b_again = slack.recorder.turns
    assert a_again["thread_id"] not in {a["thread_id"], b["thread_id"]}
    assert a_again["thread_id"].startswith("slack_C1_100_1_")
    assert b_again["thread_id"] == b["thread_id"]
    text, kwargs = slack.recorder.said[0]
    assert text.startswith("🆕 This thread now continues in a new conversation")
    assert kwargs == {"channel": "C1", "thread_ts": "100.1"}


def test_slack_stuck_thread_recovery_moves_the_thread_to_a_fresh_conversation(slack, monkeypatch) -> None:
    import row_bot.agent as agent

    stuck: list[str] = []
    healthy_turn = slack.module._stream_agent_turn_to_slack

    async def stuck_once(client, channel_id, text, config, *, thread_ts=None):
        if not stuck:
            stuck.append(config["configurable"]["thread_id"])
            raise RuntimeError("tool_calls that do not have a corresponding ToolMessage")
        return await healthy_turn(client, channel_id, text, config, thread_ts=thread_ts)

    def repair_fails(*_args: Any) -> None:
        raise RuntimeError("still stuck")

    monkeypatch.setattr(slack.module, "_stream_agent_turn_to_slack", stuck_once)
    monkeypatch.setattr(agent, "repair_orphaned_tool_calls", repair_fails)

    _mention(slack, "do the thing", "100.1")
    _mention(slack, "again", "100.2", thread_ts="100.1")

    assert stuck == ["slack_C1_100_1"]
    assert slack.recorder.turns[-1]["thread_id"].startswith("slack_C1_100_1_")
    text, kwargs = slack.recorder.said[0]
    assert "started a fresh thread" in text and kwargs["thread_ts"] == "100.1"


def test_an_approval_waiting_in_one_slack_thread_does_not_capture_another(slack) -> None:
    slack.module._store_slack_interrupt(
        channel_id="C1", interrupt_data={"tool": "x"}, config={}, thread_ts="100.1",
    )
    slack.module._store_slack_interrupt(
        channel_id="D1", interrupt_data={"tool": "x"}, config={}, thread_ts="1.1",
    )

    _mention(slack, "another topic", "200.1")
    _message(slack, "something", "1.5", channel="D1", channel_type="im")

    assert [turn["thread_ts"] for turn in slack.recorder.turns] == ["200.1"]
    assert slack.recorder.said[0][0].startswith("There's a pending approval")


@pytest.fixture
def running_slack(slack, monkeypatch):
    from row_bot.channels import registry

    def run_now(coro) -> Future:
        future: Future = Future()
        future.set_result(asyncio.run(coro))
        return future

    monkeypatch.setattr(slack.module, "_running", True)
    monkeypatch.setattr(slack.module, "_app", SimpleNamespace(client=slack.client))
    monkeypatch.setattr(slack.module, "_schedule_coro", run_now)
    registry.register(slack.module._slack_channel)
    return slack


def test_late_messages_reach_the_slack_thread_of_their_conversation(running_slack) -> None:
    from row_bot.channels.thread_notifications import deliver_parent_thread_notification

    _mention(running_slack, "start a long job", "100.1")
    thread_id = running_slack.recorder.turns[0]["thread_id"]

    delivered = deliver_parent_thread_notification(
        key="e1a-test:late", thread_id=thread_id, kind="test_late", text="The job finished.",
    )

    assert delivered is True
    assert running_slack.client.posts[-1] == {
        "channel": "C1", "text": "The job finished.", "thread_ts": "100.1",
    }


def test_task_delivery_to_a_slack_target_is_unchanged_and_can_name_a_thread(running_slack) -> None:
    status, _detail = running_slack.tasks._deliver_to_channels(
        {"name": "Digest", "channels": ["slack"], "delivery_target": "C9"}, "report",
    )
    threaded, _detail = running_slack.tasks._deliver_to_channels(
        {"name": "Digest", "channels": ["slack"], "delivery_target": "C9:500.1"}, "report",
    )

    assert (status, threaded) == ("delivered", "delivered")
    plain, in_thread = running_slack.client.posts[-2:]
    assert plain == {"channel": "C9", "text": "📋 Digest\n\nreport"}
    assert in_thread == {"channel": "C9", "text": "📋 Digest\n\nreport", "thread_ts": "500.1"}


def test_none_versus_empty_channel_delivery_is_unchanged(running_slack, monkeypatch) -> None:
    tasks = running_slack.tasks
    monkeypatch.setattr(running_slack.module._slack_channel, "get_default_target", lambda: "UDEFAULT")
    tasks.set_workflow_default_channels(["slack"])

    web_only = tasks._deliver_to_channels({"name": "Quiet", "channels": []}, "report")
    inherited = tasks._deliver_to_channels({"name": "Loud", "channels": None}, "report")

    assert web_only == ("", "")
    assert inherited[0] == "delivered"
    assert running_slack.client.posts == [{"channel": "UDEFAULT", "text": "📋 Loud\n\nreport"}]


def test_files_in_one_slack_message_become_one_turn(slack, monkeypatch) -> None:
    from row_bot.channels import media

    analysed: list[bytes] = []

    async def download(_client, file_info: dict, _url: str) -> bytes:
        return file_info["id"].encode()

    def analyze(data: bytes, _question: str) -> str:
        analysed.append(data)
        return f"a picture of {data.decode()}"

    monkeypatch.setattr(slack.module, "_download_slack_file", download)
    monkeypatch.setattr(media, "analyze_image", analyze)
    monkeypatch.setattr(media, "transcribe_audio", lambda data, _ext: f"spoken {data.decode()}")
    event = {
        "user": PERSON, "channel": "D1", "channel_type": "im", "text": "what are these?", "ts": "5.1",
        "subtype": "file_share",
        "files": [
            {"id": "cat", "name": "cat.png", "mimetype": "image/png", "url_private_download": "u1"},
            {"id": "dog", "name": "dog.png", "mimetype": "image/png", "url_private_download": "u2"},
            {"id": "memo", "name": "memo.ogg", "mimetype": "audio/ogg", "url_private_download": "u3"},
        ],
    }

    asyncio.run(slack.module._route_message_event(event, slack.recorder.say, slack.client))

    assert analysed == [b"cat", b"dog"]
    (turn,) = slack.recorder.turns
    assert turn["text"].startswith("what are these?\n\n[Voice message transcription]: spoken memo")
    assert "a picture of cat" in turn["text"] and "a picture of dog" in turn["text"]
