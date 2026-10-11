"""What the client renders reaches it outside the model's text (WP D2).

A chart renders from its result's artifact, and from the text of results stored before; a
plugin's image is published as the call's media.
"""
from __future__ import annotations

import base64
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from row_bot.api.v1.schemas import TranscriptRow
from tests.contracts.client_platform.test_headless_lifecycle import platform  # noqa: F401


pytestmark = pytest.mark.subsystem

FIGURE = {
    "data": [{"type": "bar", "name": "Visits", "x": ["Mon", "Tue", "Wed"], "y": [3, 5, 4]}],
    "layout": {"title": {"text": "Visits this week"}, "template": {"layout": {"font": {"color": "#f2f5fa"}}}},
}
LABEL = "Chart created: Visits this week (3 data points)"
CREATED = "__CHART__:" + json.dumps(FIGURE, separators=(",", ":")) + "\n\n" + LABEL


@pytest.fixture
def conversation(tmp_path, monkeypatch):
    from row_bot import tool_outputs

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(tool_outputs, "current_thread_id", lambda: "conversation-a")
    return "conversation-a"


def _store(result: ToolMessage, tool: str = "create_chart") -> None:
    from row_bot import threads

    assert threads.append_checkpoint_messages("conversation-a", [
        HumanMessage(content="Chart my visits.", id="ask"),
        AIMessage(content="", id="calls", tool_calls=[{"id": result.tool_call_id, "name": tool, "args": {}}]),
        result,
        AIMessage(content="Here it is.", id="answer"),
    ])


def _rows(platform) -> tuple[dict, dict]:
    rows = platform.transcript("conversation-a")["rows"]
    for row in rows:
        TranscriptRow.model_validate(row)
    return next(row for row in rows if row["role"] == "tool"), next(row for row in rows if row["id"].endswith(":calls"))


def test_a_new_chart_renders_from_its_artifact_while_the_model_kept_only_a_summary(platform, conversation):
    from row_bot import threads, tool_outputs

    created = tool_outputs.cap_tool_message(ToolMessage(content=CREATED, name="create_chart", tool_call_id="call-chart",
                                                        id="chart-result"))
    _store(created)

    stored = next(m for m in threads.get_latest_checkpoint_messages("conversation-a") if m.type == "tool")
    assert "__CHART__" not in stored.content and "template" not in stored.content
    tool_row, parent = _rows(platform)

    (chart,) = [block for block in tool_row["blocks"] if block["type"] == "chart"]
    assert json.loads(chart["figure_json"]) == FIGURE
    assert chart["text"] == LABEL
    # The step under the parent shows what the model was told.
    (item,) = [item for group in parent["traces"] for item in group["items"]]
    assert item["safe_summary"].startswith(LABEL)
    assert "Chart out_" in item["safe_summary"]


def test_a_chart_stored_before_still_renders_from_its_text(platform, conversation):
    _store(ToolMessage(content=CREATED, name="create_chart", tool_call_id="call-chart", id="chart-result"))

    tool_row, _parent = _rows(platform)

    (chart,) = [block for block in tool_row["blocks"] if block["type"] == "chart"]
    assert json.loads(chart["figure_json"]) == FIGURE
    assert chart["text"] == LABEL


def test_a_chart_too_large_for_its_row_is_left_out_as_before_and_the_summary_kept(platform, conversation):
    from row_bot.rendered_results import CHART_ARTIFACT_KEY

    points = list(range(40_000))
    figure = json.dumps({"data": [{"type": "scatter", "x": points, "y": points}], "layout": {}})
    _store(ToolMessage(content="A summary for the model.", name="create_chart", tool_call_id="call-chart",
                       id="chart-result", artifact={CHART_ARTIFACT_KEY: {"figure": figure, "label": "Big"}}))

    tool_row, _parent = _rows(platform)

    assert tool_row.get("content_status") != "lazy"
    assert [block["type"] for block in tool_row["blocks"]] == ["markdown"]
    assert tool_row["blocks"][0]["text"] == "A summary for the model."


def test_a_plugins_image_is_published_as_media_and_the_model_reads_its_caption(platform, conversation):
    from langchain_core.tools import tool
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    from row_bot.agent import _wrap_platform_tool_call
    from row_bot.application.attachment_context import prepared_attachments
    from row_bot.application.attachments import read_attachment
    from row_bot.plugins.api import PluginTool

    png = b"\x89PNG\r\n\x1a\n" + b"lighthouse" * 2_000

    @tool
    def plugin_draw(subject: str) -> str:
        """Draw a synthetic fixture image."""
        return PluginTool.image_result(base64.b64encode(png).decode(), f"A {subject} at dusk")

    graph = StateGraph(MessagesState)
    graph.add_node("tools", ToolNode([plugin_draw], wrap_tool_call=_wrap_platform_tool_call))
    graph.add_edge(START, "tools")
    graph.add_edge("tools", END)
    call = {"id": "call-draw", "name": "plugin_draw", "args": {"subject": "lighthouse"}, "type": "tool_call"}
    with prepared_attachments("conversation-a", []):
        result = graph.compile().invoke({"messages": [AIMessage(content="", tool_calls=[call])]})["messages"][-1]

    assert result.content == ("A lighthouse at dusk\n"
                              "[Image shown to the person (about 20 KB); its data is not repeated here.]")
    (outcome,) = result.additional_kwargs["platform_media"]
    assert read_attachment(outcome["payload"]["media_ref"])[1] == png
    # Kept with the result, so the image shows again when the conversation is reopened.
    _store(result.model_copy(update={"id": "draw-result"}), tool="plugin_draw")
    _tool_row, parent = _rows(platform)
    (item,) = [item for group in parent["traces"] for item in group["items"]]
    assert [media["media_ref"] for media in item["specialization"]["media"]] == [outcome["payload"]["media_ref"]]
