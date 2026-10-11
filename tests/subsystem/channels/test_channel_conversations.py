"""Which conversation a channel chat talks to, and /new (decision D6).

DMs, SMS contacts, WhatsApp chats and plugin chats keep one rolling
conversation; /new moves them to a fresh one, the earlier one stays, and a
chat that talked to Row-Bot before keeps its conversation until /new.
"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace
from typing import Any

import pytest

from row_bot.channels.streaming import ChannelDeliveryResult


pytestmark = pytest.mark.subsystem

PHONE = "+15550001111"
WA_CHAT = "15550002222@s.whatsapp.net"


@pytest.fixture
def store(tmp_path, reload_for_data_dir):
    tasks, threads = reload_for_data_dir(tmp_path / "data", "row_bot.tasks", "row_bot.threads")
    return SimpleNamespace(tasks=tasks, threads=threads)


def _sms_conversation():
    from row_bot.channels.conversations import ChannelConversation

    return ChannelConversation(
        channel="sms",
        key=PHONE,
        default_thread_id="sms_15550001111",
        name=f"📱 SMS – {PHONE}",
        target=PHONE,
    )


def _thread_name(threads, thread_id: str) -> str:
    with sqlite3.connect(threads.DB_PATH) as conn:
        row = conn.execute("SELECT name FROM thread_meta WHERE thread_id = ?", (thread_id,)).fetchone()
    return str(row[0]) if row else ""


def _thread_count(threads) -> int:
    with sqlite3.connect(threads.DB_PATH) as conn:
        return int(conn.execute("SELECT COUNT(*) FROM thread_meta").fetchone()[0])


# ── The pointer ──────────────────────────────────────────────────────


def test_a_chat_from_before_keeps_its_conversation_and_where_it_delivers(store) -> None:
    from row_bot.channels import conversations

    store.threads._save_thread_meta("sms_15550001111", "My renamed SMS chat")

    thread_id = conversations.current_thread(_sms_conversation())

    assert thread_id == "sms_15550001111"
    assert store.tasks.get_thread_channel_ref(thread_id)["target"] == PHONE
    # Continuing a conversation keeps the name the person gave it.
    assert _thread_name(store.threads, thread_id) == "My renamed SMS chat"


def test_a_new_chat_starts_with_its_familiar_thread_id(store) -> None:
    from row_bot.channels import conversations

    thread_id = conversations.current_thread(_sms_conversation())

    assert thread_id == "sms_15550001111"
    assert conversations.current_thread(_sms_conversation()) == thread_id
    assert _thread_count(store.threads) == 1


def test_new_moves_the_chat_on_and_keeps_the_earlier_conversation(store) -> None:
    from row_bot.channels import conversations

    first = conversations.current_thread(_sms_conversation())
    second = conversations.start_new_thread(_sms_conversation())

    assert second != first and second.startswith("sms_15550001111_")
    assert conversations.current_thread(_sms_conversation()) == second
    assert store.threads._thread_exists(first)
    assert store.tasks.get_thread_channel_ref(second)["target"] == PHONE
    assert _thread_name(store.threads, second).startswith(f"📱 SMS – {PHONE} · ")


def test_the_current_conversation_survives_a_restart(store, tmp_path, reload_for_data_dir) -> None:
    from row_bot.channels import conversations

    conversations.current_thread(_sms_conversation())
    moved = conversations.start_new_thread(_sms_conversation())
    reload_for_data_dir(tmp_path / "data", "row_bot.tasks", "row_bot.threads")

    assert conversations.current_thread(_sms_conversation()) == moved


def test_a_deleted_conversation_is_replaced_never_by_the_one_the_chat_left(store) -> None:
    from row_bot.channels import conversations

    legacy = conversations.current_thread(_sms_conversation())
    moved = conversations.start_new_thread(_sms_conversation())
    deleted, failures = store.threads.delete_threads([moved])
    assert deleted == 1 and not failures

    fresh = conversations.current_thread(_sms_conversation())

    assert fresh not in {legacy, moved}
    assert store.threads._thread_exists(legacy)
    assert store.tasks.get_thread_channel_ref(fresh)["target"] == PHONE


def test_only_a_thread_with_a_live_conversation_is_known(store) -> None:
    from row_bot.channels import conversations

    assert conversations.known_thread("sms", PHONE) == ""
    thread_id = conversations.current_thread(_sms_conversation())
    assert conversations.known_thread("sms", PHONE) == thread_id
    store.threads.delete_threads([thread_id])
    assert conversations.known_thread("sms", PHONE) == ""


# ── Generic /new ─────────────────────────────────────────────────────


def test_generic_new_without_a_rotation_reports_truthfully_and_creates_nothing(store) -> None:
    from row_bot.channels import commands

    before = _thread_count(store.threads)

    reply = commands.dispatch("Fake Channel", "/new")

    assert reply == "Fake Channel can't start a new conversation from here."
    assert _thread_count(store.threads) == before


def test_generic_new_runs_the_adapters_rotation_once() -> None:
    from row_bot.channels import commands

    calls: list[str] = []

    assert commands.dispatch("slack", "/new", new_conversation=lambda: calls.append("x")) == (
        commands.NEW_CONVERSATION_STARTED
    )
    assert commands.dispatch("slack", "/newthread", new_conversation=lambda: "In this thread.") == (
        "In this thread."
    )
    assert calls == ["x"]
    # Other commands never move the conversation.
    commands.dispatch("slack", "/status", new_conversation=lambda: calls.append("y"))
    assert calls == ["x"]


def test_generic_new_says_when_the_rotation_failed() -> None:
    from row_bot.channels import commands

    def broken() -> None:
        raise RuntimeError("store unavailable")

    reply = commands.dispatch("sms", "/new", new_conversation=broken)

    assert reply.startswith("❌ Could not start a new conversation")


# ── SMS ──────────────────────────────────────────────────────────────


class _SMSRequest:
    def __init__(self, body: str, sid: str) -> None:
        self.headers = {"content-length": "0"}
        self.client = SimpleNamespace(host="127.0.0.1")
        self.url = "https://example.invalid/sms"
        self._form = {"From": PHONE, "Body": body, "MessageSid": sid}

    async def form(self) -> dict[str, str]:
        return dict(self._form)


@pytest.fixture
def sms(store, monkeypatch):
    from row_bot.channels import sms

    replies: list[str] = []
    turns: list[str] = []

    async def signed(_request, _client_ip):
        return None

    def run_agent(text: str, config: dict):
        turns.append(config["configurable"]["thread_id"])
        return "answer", None, []

    monkeypatch.setattr(sms, "_running", True)
    monkeypatch.setattr(sms, "_refuse_unsigned", signed)
    monkeypatch.setattr(sms, "_is_authorised", lambda _phone: True)
    monkeypatch.setattr(sms, "_send_reply", lambda _phone, text: replies.append(text))
    monkeypatch.setattr(sms, "_run_agent_sync", run_agent)
    monkeypatch.setattr(sms, "_pending_interrupts", {})
    monkeypatch.setattr(sms, "_pending_task_approvals", {})
    sms._rate_limits.clear()
    sms._seen_sids.clear()
    return SimpleNamespace(module=sms, replies=replies, turns=turns)


def _text_sms(sms, body: str, sid: str) -> None:
    asyncio.run(sms.module._handle_inbound_sms(_SMSRequest(body, sid)))


def test_sms_new_starts_a_fresh_conversation_for_the_contact(sms, store) -> None:
    store.threads._save_thread_meta("sms_15550001111", "📱 SMS – old")
    _text_sms(sms, "hello", "sid-1")
    _text_sms(sms, "/new", "sid-2")
    _text_sms(sms, "hello again", "sid-3")

    assert sms.turns[0] == "sms_15550001111"
    assert sms.turns[1] != "sms_15550001111"
    assert sms.turns[1].startswith("sms_15550001111_")
    assert sms.replies[1] == "🆕 Started a new conversation. The earlier one stays in Row-Bot."
    assert store.threads._thread_exists("sms_15550001111")


def test_sms_stuck_thread_recovery_moves_the_contact_to_a_fresh_conversation(
    sms, store, monkeypatch,
) -> None:
    import row_bot.agent as agent

    stuck: list[str] = []

    def stuck_then_fine(text: str, config: dict):
        thread_id = config["configurable"]["thread_id"]
        sms.turns.append(thread_id)
        if not stuck:
            stuck.append(thread_id)
            raise RuntimeError("tool_calls that do not have a corresponding ToolMessage")
        return "answer", None, []

    def repair_fails(*_args: Any) -> None:
        raise RuntimeError("tool_calls that do not have a corresponding ToolMessage")

    monkeypatch.setattr(sms.module, "_run_agent_sync", stuck_then_fine)
    monkeypatch.setattr(agent, "repair_orphaned_tool_calls", repair_fails)

    _text_sms(sms, "hello", "sid-1")
    _text_sms(sms, "hello again", "sid-2")

    assert "started a fresh thread" in sms.replies[0]
    assert sms.turns[-1] != stuck[0]
    assert store.threads._thread_exists(stuck[0])


# ── WhatsApp ─────────────────────────────────────────────────────────


def test_whatsapp_new_starts_a_fresh_conversation_for_the_chat(store, monkeypatch) -> None:
    from row_bot.channels import whatsapp

    sent: list[str] = []
    turns: list[str] = []

    def streamed_turn(_chat_id: str, _text: str, config: dict):
        turns.append(config["configurable"]["thread_id"])
        return "answer", None, [], [], ChannelDeliveryResult(
            delivered=True, streamed=True, finalized=True, fallback_sent=False,
            transport="whatsapp:edit", final_text="answer",
        )

    monkeypatch.setattr(whatsapp, "_is_authorised", lambda _chat: True)
    monkeypatch.setattr(whatsapp, "_send_message_sync", lambda _chat, text, **_kw: sent.append(text))
    monkeypatch.setattr(whatsapp, "_typing_sync", lambda *_: None)
    monkeypatch.setattr(whatsapp, "_react_sync", lambda *_: None)
    monkeypatch.setattr(whatsapp, "_run_whatsapp_stream_turn_sync", streamed_turn)
    monkeypatch.setattr(whatsapp, "_pending_interrupts", {})
    monkeypatch.setattr(whatsapp, "_pending_task_approvals", {})

    whatsapp._process_inbound({"from": WA_CHAT, "body": "hello"})
    whatsapp._process_inbound({"from": WA_CHAT, "body": "/new"})
    whatsapp._process_inbound({"from": WA_CHAT, "body": "hello again"})

    legacy = whatsapp._make_thread_id(WA_CHAT)
    assert turns[0] == legacy
    assert turns[1] != legacy and turns[1].startswith(legacy + "_")
    assert sent == ["🆕 Started a new conversation. The earlier one stays in Row-Bot."]
    assert store.tasks.get_thread_channel_ref(turns[1])["target"] == WA_CHAT


def test_whatsapp_stuck_thread_moves_the_chat_on_without_running_the_turn_again(
    store, monkeypatch,
) -> None:
    from row_bot.channels import whatsapp

    sent: list[str] = []
    turns: list[str] = []

    def stuck_once(_chat_id: str, _text: str, config: dict):
        turns.append(config["configurable"]["thread_id"])
        if len(turns) == 1:
            raise RuntimeError("tool_call_ids did not have response messages")
        return "answer", None, [], [], ChannelDeliveryResult(
            delivered=True, streamed=True, finalized=True, fallback_sent=False,
            transport="whatsapp:edit", final_text="answer",
        )

    monkeypatch.setattr(whatsapp, "_is_authorised", lambda _chat: True)
    monkeypatch.setattr(whatsapp, "_send_message_sync", lambda _chat, text, **_kw: sent.append(text))
    monkeypatch.setattr(whatsapp, "_typing_sync", lambda *_: None)
    monkeypatch.setattr(whatsapp, "_react_sync", lambda *_: None)
    monkeypatch.setattr(whatsapp, "_run_whatsapp_stream_turn_sync", stuck_once)
    monkeypatch.setattr(whatsapp, "_pending_interrupts", {})
    monkeypatch.setattr(whatsapp, "_pending_task_approvals", {})

    whatsapp._process_inbound({"from": WA_CHAT, "body": "hello"})
    whatsapp._process_inbound({"from": WA_CHAT, "body": "hello again"})

    stuck, fresh = turns
    assert fresh != stuck
    assert "started a fresh thread" in sent[0]
    assert store.threads._thread_exists(stuck)


# ── Plugin channels ──────────────────────────────────────────────────


def test_plugin_channel_new_moves_the_chat_to_a_fresh_conversation(store, monkeypatch) -> None:
    import row_bot.agent as agent
    from row_bot.channels import runtime as channel_runtime
    from row_bot.plugins.api import ChannelInboundMessage, ChannelOutboundCallbacks
    from row_bot.plugins.channel_runtime import handle_plugin_channel_message
    from row_bot.tools import registry as tool_registry

    async def no_goal(**_kwargs: Any):
        return channel_runtime.ChannelGoalRunResult(turns=0, status="idle", reason="")

    def fake_stream_agent(_text: str, _enabled: list[str], _config: dict):
        yield ("token", "answer")
        yield ("done", "answer")

    monkeypatch.setattr(tool_registry, "get_enabled_tools", lambda: [])
    monkeypatch.setattr(channel_runtime, "continue_channel_goal_after_turn_async", no_goal)
    monkeypatch.setattr(agent, "stream_agent", fake_stream_agent)
    texts: list[str] = []

    def send(text: str) -> None:
        texts.append(text)

    def run(text: str):
        return asyncio.run(handle_plugin_channel_message(
            plugin_id="teams-plugin",
            message=ChannelInboundMessage(
                channel_name="teams", external_conversation_id="conversation-1",
                sender_id="user-1", text=text,
            ),
            callbacks=ChannelOutboundCallbacks(send_text=send),
            channel=None,
            enabled_tool_names=[],
            stream=False,
        ))

    first = run("hello")
    command = run("/new")
    after = run("hello again")

    assert first.thread_id == "teams_conversation-1"
    assert command.answer == "🆕 Started a new conversation. The earlier one stays in Row-Bot."
    assert after.thread_id.startswith("teams_conversation-1_")
    assert store.threads._thread_exists("teams_conversation-1")
