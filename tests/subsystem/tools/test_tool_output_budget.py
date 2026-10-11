"""A long tool result is shortened once, when created, and the rest stays readable by handle (WP D1)."""
from __future__ import annotations

import re

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool

from row_bot import tool_outputs


pytestmark = pytest.mark.subsystem


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    folder = tmp_path / "data"
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(folder))
    return folder


@pytest.fixture
def conversation(monkeypatch):
    """The conversation the running tool call belongs to."""
    current = {"thread_id": "thread-a"}
    monkeypatch.setattr(tool_outputs, "current_thread_id", lambda: current["thread_id"])
    return current


def _numbered(count: int) -> str:
    return "".join(f"line {number:06d} of the command output\n" for number in range(1, count + 1))


def _handle_and_offset(shortened: str) -> tuple[str, int]:
    match = re.search(r'read_tool_output\(handle="(out_[0-9a-f]+)", offset=(\d+)\)', shortened)
    assert match, shortened[:400]
    return match.group(1), int(match.group(2))


def _read_to_the_end(handle: str, offset: int) -> str:
    """Page through a saved output the way the model does: follow 'Continue with offset=N'."""
    parts: list[str] = []
    for _ in range(100):
        page = tool_outputs.read_tool_output(handle, offset=offset)
        header, _, body = page.partition("\n")
        parts.append(body)
        if "End of output." in header:
            return "".join(parts)
        offset = int(re.search(r"Continue with offset=(\d+)", header).group(1))
    raise AssertionError("never reached the end")


def test_a_long_result_shows_its_head_and_tail_and_a_handle_reads_the_rest(data_dir, conversation):
    text = _numbered(12_000)  # ~430K characters

    shortened = tool_outputs.bounded(text, tool_name="run_command", max_chars=30_000)

    assert len(shortened) < 31_000
    # The notice comes first, so it survives any later shortening of the result's end.
    assert shortened.startswith("[run_command output is ")
    assert "line 000001 " in shortened and "line 012000 " in shortened
    handle, offset = _handle_and_offset(shortened)
    head = shortened.split("\n", 1)[1][:offset]
    assert text.startswith(head)
    assert head + _read_to_the_end(handle, offset) == text


def test_a_result_that_fits_is_returned_unchanged(data_dir, conversation):
    assert tool_outputs.bounded("short", tool_name="run_command", max_chars=100) == "short"
    assert not (data_dir / "tool_outputs").exists()


def test_saved_outputs_are_read_only_in_their_own_conversation(data_dir, conversation):
    handle, _ = _handle_and_offset(tool_outputs.bounded(_numbered(3_000), tool_name="t", max_chars=5_000))

    conversation["thread_id"] = "thread-b"

    assert "No saved output" in tool_outputs.read_tool_output(handle)


@pytest.mark.parametrize("handle", ["../threads.db", "out_../../x", "OUT_0123456789abcdef", "", "out_123"])
def test_the_reader_takes_only_handles(data_dir, conversation, handle):
    assert tool_outputs.read_tool_output(handle).startswith("Unknown handle")


def test_a_conversation_id_with_a_colon_still_gets_its_own_folder(data_dir, conversation):
    conversation["thread_id"] = "agent:child:1"
    handle, offset = _handle_and_offset(tool_outputs.bounded(_numbered(3_000), tool_name="t", max_chars=5_000))

    (folder,) = (data_dir / "tool_outputs").iterdir()
    assert ":" not in folder.name
    assert "End of output." in tool_outputs.read_tool_output(handle, offset=offset, limit=1_000_000)


def test_a_tool_message_is_capped_when_created_and_rendered_results_are_left_alone(data_dir, conversation):
    big = _numbered(5_000)  # ~180K characters
    capped = tool_outputs.cap_tool_message(ToolMessage(content=big, name="custom_plugin_tool", tool_call_id="c1"))

    assert len(capped.content) <= tool_outputs.RESULT_CAP_CHARS + 1_000
    assert capped.tool_call_id == "c1" and capped.name == "custom_plugin_tool"
    handle, offset = _handle_and_offset(capped.content)
    assert big.startswith(capped.content.split("\n", 1)[1][:offset])
    assert _read_to_the_end(handle, offset) == big[offset:]

    chart = "__CHART__:" + "{" + "1" * 200_000 + "}\n\nChart created"
    assert tool_outputs.cap_tool_message(ToolMessage(content=chart, name="create_chart", tool_call_id="c2")).content == chart
    page = "x" * 200_000
    for name in (tool_outputs.READER_TOOL_NAME, "agent_wait", "delegate_work"):
        message = ToolMessage(content=page, name=name, tool_call_id="c3")
        assert tool_outputs.cap_tool_message(message).content == page


def test_text_blocks_are_capped_and_images_kept(data_dir, conversation):
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    message = ToolMessage(
        content=[{"type": "text", "text": _numbered(5_000)}, image],
        name="browser_snapshot",
        tool_call_id="c1",
    )

    capped = tool_outputs.cap_tool_message(message)

    assert len(capped.content[0]["text"]) <= tool_outputs.RESULT_CAP_CHARS + 1_000
    assert capped.content[1] == image


def test_every_tool_result_the_agent_creates_passes_the_cap(data_dir, conversation, monkeypatch):
    """Through the Agent's own ToolNode wrapper: plugins, custom tools and MCP included."""
    import row_bot.agent as agent
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    def plugin_dump(query: str) -> str:
        return _numbered(6_000)

    node = ToolNode(
        [StructuredTool.from_function(plugin_dump, name="plugin_dump", description="Dump.")],
        wrap_tool_call=agent._wrap_platform_tool_call,
    )
    builder = StateGraph(MessagesState)
    builder.add_node("tools", node)
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    call = {"id": "call-1", "name": "plugin_dump", "args": {"query": "all"}, "type": "tool_call"}

    result = builder.compile().invoke({"messages": [AIMessage(content="", tool_calls=[call])]})["messages"][-1]

    assert len(result.content) <= tool_outputs.RESULT_CAP_CHARS + 1_000
    handle, offset = _handle_and_offset(result.content)
    assert _read_to_the_end(handle, offset).endswith("line 006000 of the command output\n")
    assert tool_outputs.saved_output_source(handle) == {"tools": ["plugin_dump"], "external": False, "fenced": False}


def test_a_saved_output_remembers_a_plugin_or_mcp_source(data_dir, conversation, monkeypatch):
    from row_bot.plugins import registry as plugin_registry

    monkeypatch.setattr(plugin_registry, "get_plugin_tool_names", lambda: ["crm_lookup"])
    monkeypatch.setattr(plugin_registry, "get_enabled_plugin_tool_records",
                        lambda: [{"runtime_name": "custom_tool_report"}])
    big = _numbered(5_000)

    for name in ("crm_lookup", "custom_tool_report", "mcp_notes_read_page", "wiki_read"):
        capped = tool_outputs.cap_tool_message(ToolMessage(content=big + name, name=name, tool_call_id="c1"))
        handle, _offset = _handle_and_offset(capped.content)
        assert tool_outputs.saved_output_source(handle)["external"] is (name != "wiki_read"), name

    # A tool_invoke call is recorded under the tool it ran.
    invoked = tool_outputs.cap_tool_message(
        ToolMessage(content=big + "invoked", name="tool_invoke", tool_call_id="c2"), source="crm_lookup")
    handle, _offset = _handle_and_offset(invoked.content)
    assert tool_outputs.saved_output_source(handle) == {"tools": ["crm_lookup"], "external": True, "fenced": False}
    assert tool_outputs.saved_output_source("out_0123456789abcdef") is None


def test_a_paged_text_reaches_its_last_line(data_dir, conversation):
    text = _numbered(7_000)
    pages: list[str] = []
    offset = 1
    while True:
        page, footer = tool_outputs.page_lines(text, offset=offset, limit=2_000, max_chars=50_000)
        pages.append(page)
        if not footer or "End of text." in footer:
            break
        offset = int(re.search(r"Continue with offset=(\d+)", footer).group(1))

    assert "".join(pages) == text
    assert len(pages) > 2


def test_a_line_longer_than_a_page_is_cut_and_the_rest_stays_readable(data_dir, conversation):
    minified = "x" * 120_000 + "\n" + "tail line\n"

    page, footer = tool_outputs.page_lines(minified, offset=1, max_chars=50_000)

    assert len(page) <= 50_001
    handle, offset = _handle_and_offset(footer)
    assert offset == 50_000
    assert page.rstrip("\n") + _read_to_the_end(handle, offset) == minified


def test_saved_outputs_go_with_their_conversation(monkeypatch):
    import uuid

    from row_bot import threads
    from row_bot.thread_cleanup import delete_thread

    gone = threads.create_thread("Tool output fixture", thread_id=f"outputs-{uuid.uuid4().hex[:8]}",
                                 seed_default_skills=False)
    monkeypatch.setattr(tool_outputs, "current_thread_id", lambda: gone)
    handle, _ = _handle_and_offset(tool_outputs.bounded(_numbered(3_000), tool_name="t", max_chars=5_000))
    folder = tool_outputs.outputs_root() / tool_outputs.thread_folder_name(gone)
    assert (folder / f"{handle}.txt").exists()

    assert delete_thread(gone).deleted is True

    assert not folder.exists()


def test_orphaned_outputs_are_swept_but_recent_ones_kept(data_dir):
    root = tool_outputs.outputs_root()
    for name in ("kept", "orphan", "fresh-orphan"):
        (root / name).mkdir(parents=True)
        (root / name / "out_0123456789abcdef.txt").write_text("x", encoding="utf-8")
    import os
    import time

    old = time.time() - 3 * 86_400
    os.utime(root / "orphan", (old, old))
    os.utime(root / "kept", (old, old))

    removed = tool_outputs.sweep_orphaned_outputs(["kept"], older_than=time.time() - 86_400)

    assert removed == 1
    assert sorted(path.name for path in root.iterdir()) == ["fresh-orphan", "kept"]


def test_a_conversation_keeps_only_its_newest_saved_outputs(data_dir, conversation, monkeypatch):
    import os
    import time

    monkeypatch.setattr(tool_outputs, "MAX_SAVED_PER_THREAD", 3)
    folder = tool_outputs.outputs_root() / "thread-a"
    folder.mkdir(parents=True)
    now = time.time()
    for age in range(4):  # four older outputs, oldest first
        old = folder / f"out_{age:016x}.txt"
        old.write_bytes(b"older output")
        os.utime(old, (now - 1_000 + age, now - 1_000 + age))

    handle = tool_outputs.spill(_numbered(10))

    kept = sorted(path.name for path in folder.glob("out_*.txt"))
    assert kept == [f"out_{2:016x}.txt", f"out_{3:016x}.txt", f"{handle}.txt"]


def test_compact_json_drops_empty_fields_but_keeps_false_and_zero():
    payload = {"ok": False, "count": 0, "name": "", "items": [], "nested": {"a": None, "b": "x"}, "list": [{}]}

    assert tool_outputs.compact_json(payload) == '{"ok":false,"count":0,"nested":{"b":"x"},"list":[{}]}'
