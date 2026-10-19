from __future__ import annotations

import base64
import json
import ssl
import threading
from typing import Any

from langchain_core.messages import AIMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime

from row_bot.tools.gmail_tool import GmailTool, _CreateDraftInput, _SendMessageInput


def _tool(name: str, schema) -> StructuredTool:
    def _run(**kwargs: Any) -> str:
        return str(kwargs)

    return StructuredTool.from_function(
        func=_run,
        name=name,
        description=f"{name} schema contract",
        args_schema=schema,
    )


def test_gmail_recipient_fields_advertise_typed_arrays() -> None:
    for schema in (_CreateDraftInput, _SendMessageInput):
        properties = schema.model_json_schema()["properties"]
        assert properties["to"]["type"] == "array"
        assert properties["to"]["items"] == {"type": "string"}
        for field_name in ("cc", "bcc"):
            array_branch = next(
                branch
                for branch in properties[field_name]["anyOf"]
                if branch.get("type") == "array"
            )
            assert array_branch["items"] == {"type": "string"}


def test_gmail_recipient_fields_keep_legacy_scalar_inputs() -> None:
    model = _CreateDraftInput.model_validate({
        "message": "Hello",
        "subject": "Subject",
        "to": "to@example.com",
        "cc": "cc@example.com",
        "bcc": ["bcc@example.com"],
    })

    assert model.to == ["to@example.com"]
    assert model.cc == ["cc@example.com"]
    assert model.bcc == ["bcc@example.com"]


def test_gmail_tools_retain_items_after_locked_google_conversion() -> None:
    from google.genai import types
    from langchain_google_genai._function_utils import convert_to_genai_function_declarations

    converted_tools = convert_to_genai_function_declarations([
        _tool("create_gmail_draft", _CreateDraftInput),
        _tool("send_gmail_message", _SendMessageInput),
    ])
    declarations = [
        declaration
        for converted_tool in converted_tools
        for declaration in (converted_tool.function_declarations or ())
    ]

    for declaration in declarations:
        properties = declaration.parameters.model_dump(exclude_none=True)["properties"]
        for field_name in ("to", "cc", "bcc", "attachments"):
            assert properties[field_name]["type"] == types.Type.ARRAY
            assert properties[field_name]["items"]["type"] == types.Type.STRING


class _FakeGmailService:
    """A Gmail service whose connection, like httplib2's, must never be used by two threads at once."""

    def __init__(self, backend: "_FakeGmailBackend") -> None:
        self.backend = backend
        self.in_use = threading.Lock()

    def users(self) -> "_FakeGmailService":
        return self

    def messages(self) -> "_FakeGmailService":
        return self

    def get(self, *, userId: str, format: str, id: str) -> "_FakeGmailService":
        self.message_id = id
        return self

    def execute(self) -> dict[str, Any]:
        if not self.in_use.acquire(blocking=False):
            self.backend.shared_connection_uses += 1
            raise ssl.SSLError("record layer failure")
        try:
            try:
                self.backend.overlap.wait(timeout=2)
            except threading.BrokenBarrierError:
                pass
            raw = f"Subject: Message {self.message_id}\nFrom: a@example.com\n\nBody {self.message_id}"
            return {
                "raw": base64.urlsafe_b64encode(raw.encode()).decode(),
                "threadId": f"thread-{self.message_id}",
                "snippet": f"Body {self.message_id}",
            }
        finally:
            self.in_use.release()


class _FakeGmailBackend:
    def __init__(self, parallel_calls: int) -> None:
        self.overlap = threading.Barrier(parallel_calls)
        self.shared_connection_uses = 0
        self.services: list[_FakeGmailService] = []

    def build(self) -> _FakeGmailService:
        service = _FakeGmailService(self)
        self.services.append(service)
        return service


def test_parallel_gmail_reads_never_share_a_connection(monkeypatch) -> None:
    backend = _FakeGmailBackend(parallel_calls=4)
    tool = GmailTool()
    monkeypatch.setattr(GmailTool, "has_credentials_file", lambda self: True)
    monkeypatch.setattr(GmailTool, "is_authenticated", lambda self: True)
    monkeypatch.setattr(GmailTool, "_build_api_resource", lambda self: backend.build())
    monkeypatch.setattr(tool, "get_config", lambda key, default=None: ["get_gmail_message"])
    [get_message] = tool.as_langchain_tools()
    calls = [
        {"name": "get_gmail_message", "args": {"message_id": f"m{index}"}, "id": f"call-{index}", "type": "tool_call"}
        for index in range(4)
    ]

    result = ToolNode([get_message]).invoke(
        {"messages": [AIMessage(content="", tool_calls=calls)]},
        {"configurable": {"__pregel_runtime": Runtime()}},
    )

    assert backend.shared_connection_uses == 0
    bodies = [json.loads(message.content)["body"] for message in result["messages"]]
    assert sorted(bodies) == [f"Body m{index}" for index in range(4)]


class _Request:
    def __init__(self, result) -> None:
        self.result = result

    def execute(self):
        return self.result


class _Mailbox:
    """A synthetic Gmail mailbox: two messages in one thread."""

    raws = {
        "m1": "Subject: Invoice\nFrom: billing@example.com\nContent-Type: text/html\n\n<p>Total <b>42</b></p>",
        "m2": "Subject: Re: Invoice\nFrom: a@example.com\n\nPaid",
    }

    def users(self):
        return self

    def messages(self):
        return _Collection("messages", self)

    def threads(self):
        return _Collection("threads", self)


class _Collection:
    def __init__(self, kind: str, box: _Mailbox) -> None:
        self.kind, self.box = kind, box

    def list(self, *, userId: str, q: str, maxResults: int):
        assert (userId, q) == ("me", "invoice")
        found = [{"id": "m1"}, {"id": "m2"}] if self.kind == "messages" else [{"id": "t1"}]
        return _Request({self.kind: found[:maxResults]})

    def get(self, *, userId: str, id: str, format: str = "full"):
        if self.kind == "threads":
            return _Request({"id": id, "historyId": "7", "messages": [
                {"id": "m1", "snippet": "Total 42", "payload": {"secret": "not returned"}},
                {"id": "m2", "snippet": "Paid", "payload": {}}]})
        raw = base64.urlsafe_b64encode(self.box.raws[id].encode()).decode()
        return _Request({"raw": raw, "threadId": "t1", "snippet": "snippet " + id})


def test_gmail_reads_return_messages_threads_and_their_text(monkeypatch) -> None:
    tool = GmailTool()
    monkeypatch.setattr(GmailTool, "has_credentials_file", lambda self: True)
    monkeypatch.setattr(GmailTool, "is_authenticated", lambda self: True)
    monkeypatch.setattr(GmailTool, "_build_api_resource", lambda self: _Mailbox())
    monkeypatch.setattr(tool, "get_config", lambda key, default=None: ["search_gmail", "get_gmail_message", "get_gmail_thread"])
    tools = {item.name: item for item in tool.as_langchain_tools()}
    assert list(tools) == ["search_gmail", "get_gmail_message", "get_gmail_thread"]

    found = json.loads(tools["search_gmail"].invoke({"query": "invoice", "max_results": 1}))
    assert found == [{"id": "m1", "threadId": "t1", "snippet": "snippet m1", "body": "Total 42",
                      "subject": "Invoice", "sender": "billing@example.com"}]
    threads = json.loads(tools["search_gmail"].invoke({"query": "invoice", "resource": "threads"}))
    assert threads == [{"id": "t1", "messages": [{"snippet": "Total 42", "id": "m1"}, {"snippet": "Paid", "id": "m2"}]}]
    message = json.loads(tools["get_gmail_message"].invoke({"message_id": "m2"}))
    assert (message["subject"], message["body"]) == ("Re: Invoice", "Paid")
    thread = json.loads(tools["get_gmail_thread"].invoke({"thread_id": "t1"}))
    assert thread["messages"] == [{"id": "m1", "snippet": "Total 42"}, {"id": "m2", "snippet": "Paid"}]
