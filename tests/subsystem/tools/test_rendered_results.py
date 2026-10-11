"""A result the client renders keeps its payload out of the model's text (WP D2)."""
from __future__ import annotations

import base64
import json
import re

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from row_bot import rendered_results, tool_outputs


pytestmark = pytest.mark.subsystem


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    folder = tmp_path / "data"
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(folder))
    return folder


@pytest.fixture
def conversation(monkeypatch):
    monkeypatch.setattr(tool_outputs, "current_thread_id", lambda: "thread-charts")


def _rows(count: int) -> str:
    lines = ["Month,Visits,Signups"]
    lines += [f"2024-{1 + index // 28:02d}-{1 + index % 28:02d},{100 + index * 7},{index % 13}" for index in range(count)]
    return "\n".join(lines)


def _run_through_the_agent(tool, args: dict) -> ToolMessage:
    """Call *tool* through the Agent's own ToolNode wrapper, where every result is created."""
    import row_bot.agent as agent
    from langgraph.graph import END, START, MessagesState, StateGraph
    from langgraph.prebuilt import ToolNode

    builder = StateGraph(MessagesState)
    builder.add_node("tools", ToolNode([tool], wrap_tool_call=agent._wrap_platform_tool_call))
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    call = {"id": "call-chart", "name": tool.name, "args": args, "type": "tool_call"}
    return builder.compile().invoke({"messages": [AIMessage(content="", tool_calls=[call])]})["messages"][-1]


def _create_chart_tool():
    from row_bot.tools.chart_tool import ChartTool

    (tool,) = ChartTool().as_langchain_tools()
    return tool


def _read_whole(handle: str) -> str:
    parts, offset = [], 0
    while True:
        page = tool_outputs.read_tool_output(handle, offset=offset)
        header, _, body = page.partition("\n")
        parts.append(body)
        if "End of output." in header:
            return "".join(parts)
        offset = int(re.search(r"Continue with offset=(\d+)", header).group(1))


def test_a_chart_reaches_the_model_as_a_summary_and_the_client_gets_the_figure(data_dir, conversation):
    expected = _create_chart_tool().invoke({
        "chart_type": "line", "data_source": _rows(300), "x_column": "Month", "y_column": "Visits,Signups",
        "title": "Visits and signups"})
    figure_json = expected.removeprefix("__CHART__:").split("\n\n", 1)[0]

    result = _run_through_the_agent(_create_chart_tool(), {
        "chart_type": "line", "data_source": _rows(300), "x_column": "Month", "y_column": "Visits,Signups",
        "title": "Visits and signups"})

    # The model reads a few hundred characters, not the figure.
    summary = result.content
    assert len(figure_json) > 15_000 and len(summary) < 800
    assert "__CHART__" not in summary and "bdata" not in summary
    assert summary.startswith("Chart created: Visits and signups (300 data points)")
    assert 'line chart "Visits and signups"' in summary
    assert '"Visits" line, 300 points' in summary and '"Signups" line, 300 points' in summary
    assert "y 100 to 2,193" in summary and 'x "2024-01-01" … "2024-11-20"' in summary
    # The client gets the whole figure, which never reaches a provider.
    chart = rendered_results.chart_of_artifact(result.artifact)
    assert chart == (figure_json, "Chart created: Visits and signups (300 data points)")
    # The chart has an id the model can refer to and read the figure by.
    handle = re.search(r'Chart (out_[0-9a-f]{16}): shown to the person', summary).group(1)
    assert f'read_tool_output(handle="{handle}")' in summary
    assert _read_whole(handle) == figure_json
    assert tool_outputs.saved_output_source(handle) == {"tools": ["create_chart"], "external": False, "fenced": False}


def test_a_saved_chart_image_path_and_inline_data_stay_out_of_the_label(data_dir, conversation, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tool = _create_chart_tool()
    monkeypatch.setattr("plotly.graph_objects.Figure.write_image", lambda self, path, **_: open(path, "wb").close())

    result = _run_through_the_agent(tool, {
        "chart_type": "bar", "data_source": "City,Population\nLondon,9000000\nParis,2100000",
        "save_to_file": "cities"})

    label = rendered_results.chart_of_artifact(result.artifact)[1]
    assert label.startswith("Chart created: Population by City (2 data points)")
    assert "London,9000000" not in result.content
    # Where the PNG went is still the model's to use (to send it by email or a channel).
    assert "Chart saved to: " in label and "cities.png" in result.content


@pytest.mark.parametrize(
    ("chart_type", "data", "args", "expected"),
    [
        ("line", "Day,Temp\n1,10\n2,\n3,9", {}, "Traces (1): line, 3 points, x 1 to 3, y 9 to 10."),
        ("pie", "Fruit,Count\nApple,3\nPear,5", {}, "Traces (1): pie, 2 slices, total 8."),
        ("donut", "Fruit,Count\nApple,3\nPear,5", {}, "Traces (1): donut, 2 slices, total 8."),
        ("horizontal_bar", "Region,Revenue\nNorth,10\nSouth,20", {},
         'horizontal bar, 2 points, x 10 to 20, y "North" … "South"'),
        ("histogram", "v,w\n1,1\n2,1\n2,1\n3,1", {"x_column": "v"}, "histogram, 4 values, x 1 to 3"),
        ("heatmap", "a,b,c\n1,2,3\n2,4,7\n3,5,1", {}, "heatmap, 3×3 cells"),
        ("scatter", "a,b,c\n1,2,x\n2,4,y\n3,5,x", {"x_column": "a", "y_column": "b", "color_column": "c"},
         '"x" scatter, 2 points, x 1 to 3, y 2 to 5; "y" scatter, 1 point, x 2, y 4.'),
    ],
)
def test_each_chart_type_is_summarized_from_its_figure(data_dir, conversation, chart_type, data, args, expected):
    created = _create_chart_tool().invoke({"chart_type": chart_type, "data_source": data, **args})

    summary = tool_outputs.cap_tool_message(
        ToolMessage(content=created, name="create_chart", tool_call_id="c1")).content

    assert expected in summary, summary


def test_a_plugin_chart_called_through_tool_invoke_moves_too_and_reads_back_as_outside_content(
        data_dir, conversation, monkeypatch):
    from row_bot.plugins import registry as plugin_registry

    monkeypatch.setattr(plugin_registry, "get_plugin_tool_names", lambda: ["crm_pipeline_chart"])
    figure = {"data": [{"type": "bar", "name": "Deals", "x": ["Won", "Lost"], "y": [12, 5]}],
              "layout": {"title": {"text": "Pipeline"}}}
    content = "__CHART__:" + json.dumps(figure) + "\n\nPipeline this quarter"

    result = tool_outputs.cap_tool_message(
        ToolMessage(content=content, name="tool_invoke", tool_call_id="c1"), source="crm_pipeline_chart")

    assert result.content.startswith("Pipeline this quarter\nChart out_")
    assert '"Deals" bar, 2 points, x "Won" … "Lost", y 5 to 12' in result.content
    assert json.loads(rendered_results.chart_of_artifact(result.artifact)[0]) == figure
    handle = re.search(r'handle="(out_[0-9a-f]{16})"', result.content).group(1)
    assert tool_outputs.saved_output_source(handle)["external"] is True


def test_a_chart_marker_that_is_not_a_figure_stays_text_and_is_capped_like_any_result(data_dir, conversation):
    broken = "__CHART__:{" + "1" * 200_000 + "}\n\nChart created"

    result = tool_outputs.cap_tool_message(ToolMessage(content=broken, name="plugin_chart", tool_call_id="c1"))

    assert result.artifact is None
    assert len(result.content) <= tool_outputs.RESULT_CAP_CHARS + 1_000
    assert 'read_tool_output(handle="out_' in result.content


def test_a_plugins_html_is_capped_with_a_handle_like_any_text(data_dir, conversation):
    """The client does not render a plugin's HTML, so the model keeps it, bounded like any result."""
    html = "__HTML__:<table>" + "<tr><td>row</td></tr>\n" * 8_000 + "</table>\n\nA table"

    result = tool_outputs.cap_tool_message(ToolMessage(content=html, name="plugin_report", tool_call_id="c1"))

    assert len(result.content) <= tool_outputs.RESULT_CAP_CHARS + 1_000
    handle = re.search(r'handle="(out_[0-9a-f]{16})"', result.content).group(1)
    assert _read_whole(handle) == html


def _with_client(message: ToolMessage):
    """Create *message* in a call whose media a client takes; returns (result, the call's media slots)."""
    from row_bot.application import attachment_context
    from row_bot.application.attachment_context import AttachmentExecutionCaches

    caches = AttachmentExecutionCaches("conversation-a")
    token = attachment_context._current.set(caches)
    try:
        return tool_outputs.cap_tool_message(message), caches
    finally:
        attachment_context._current.reset(token)


def test_a_plugins_image_goes_to_the_person_as_media_and_the_model_reads_its_caption(data_dir, conversation):
    encoded = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 30_000).decode()
    message = ToolMessage(content=f"__IMAGE__:{encoded}\n\nA lighthouse at dusk", name="plugin_draw",
                          tool_call_id="c1")

    shown, caches = _with_client(message)

    # Published with the call's media by the Agent's ToolNode wrapper, as a generated image is.
    assert caches.pending_image == encoded
    assert shown.content == "A lighthouse at dusk\n[Image shown to the person (about 29 KB); its data is not repeated here.]"
    assert "platform_media_error" not in shown.additional_kwargs

    # Where no client takes the call's media (a headless run), the model still never reads the image data.
    headless = tool_outputs.cap_tool_message(message)
    assert headless.content == "A lighthouse at dusk\n[Image produced (about 29 KB); its data is not included here.]"


@pytest.mark.parametrize(
    ("encoded", "code", "reason"),
    [
        ("not base64 at all!", "media_unavailable", "its data is not a valid PNG or JPEG image"),
        (base64.b64encode(b"GIF89a" + b"\0" * 64).decode(), "media_unavailable",
         "its data is not a valid PNG or JPEG image"),
        ("", "media_unavailable", "its data is not a valid PNG or JPEG image"),
        (base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 64).decode(), "payload_too_large",
         "it is larger than the attachment limit"),
    ],
)
def test_an_image_that_cannot_be_shown_is_never_said_to_be_shown(data_dir, conversation, monkeypatch,
                                                                   encoded, code, reason):
    from row_bot.application import attachments

    if code == "payload_too_large":
        monkeypatch.setattr(attachments, "MAX_ATTACHMENT_BYTES", 32)
    message = ToolMessage(content=f"__IMAGE__:{encoded}\n\nA lighthouse", name="plugin_draw", tool_call_id="c1")

    result, caches = _with_client(message)

    assert result.content == f"A lighthouse\n[The image could not be shown: {reason}.]"
    # The person sees the step's media error instead, live and when the conversation is reopened.
    assert caches.pending_image is None
    assert result.additional_kwargs["platform_media_error"] == code


def test_a_moved_or_capped_result_keeps_its_error_status(data_dir, conversation):
    chart = "__CHART__:" + json.dumps({"data": [{"type": "bar", "x": ["a"], "y": [1]}]}) + "\n\nChart created"
    long = "Traceback line\n" * 10_000

    for content in (chart, long):
        result = tool_outputs.cap_tool_message(
            ToolMessage(content=content, name="plugin_tool", tool_call_id="c1", status="error"))
        assert result.content != content
        assert result.status == "error"


def test_results_without_a_rendered_payload_are_unchanged(data_dir, conversation):
    message = ToolMessage(content="42", name="calculate", tool_call_id="c1")

    assert tool_outputs.cap_tool_message(message) is message
