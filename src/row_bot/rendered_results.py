"""Results the person's client renders: the client gets the payload, the model a summary (WP D2).

A chart's Plotly figure (``__CHART__:<json>``) is 7K characters for three bars and grows with the
data. Kept in the ToolMessage's text, it went to the model on every later call of the
conversation. When a result is created (``tool_outputs.cap_tool_message``):

* a chart's figure moves to the ToolMessage's ``artifact``. LangChain never sends an artifact to a
  provider, and the checkpoint keeps it, so the transcript projection reads the figure there
  (``runtime.checkpoint_reader``, ``message_projection``). The text becomes a short summary of the
  chart (type, title, axes, traces with their points and ranges) and a handle: ``read_tool_output``
  returns the whole figure when the model needs it;
* a plugin's image (``__IMAGE__:<base64>``) goes to the person the way generated images do (the
  call's pending image, published as media), and the model gets its caption.

Results stored before this change keep the figure in their text; the projection still reads them.
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import math
import re
import struct
from typing import Any

logger = logging.getLogger(__name__)

CHART_MARKER = "__CHART__:"
IMAGE_MARKER = "__IMAGE__:"
# The ToolMessage artifact key that holds a chart for the client: {"figure": <json>, "label": <text>}.
CHART_ARTIFACT_KEY = "row_bot_chart"
# The client shows at most this much of a chart's own text (transcript_blocks keeps the same bound).
LABEL_CHARS = 4_096
_LISTED_TRACES = 12
_TRACES_CHARS = 1_500
_NAME_CHARS = 80
# Plotly's typed arrays ({"dtype", "bdata"}): little-endian values, base64-encoded.
_TYPED_ARRAY_FORMATS = {
    "f8": "d", "f4": "f", "i1": "b", "u1": "B", "i2": "h", "u2": "H", "i4": "i", "u4": "I",
    "i8": "q", "u8": "Q",
}


def split_marker(text: str, marker: str) -> tuple[str, str] | None:
    """(payload, the text after it) of a result that starts with *marker*; the payload ends at a blank line."""
    if not text.startswith(marker):
        return None
    end = text.find("\n\n", len(marker))
    if end < 0:
        return text[len(marker):], ""
    return text[len(marker):end], text[end + 2:].strip()


def chart_of_artifact(artifact: Any) -> tuple[str, str] | None:
    """(figure JSON, label) of a chart a ToolMessage artifact holds for the client, or None."""
    if not isinstance(artifact, dict):
        return None
    chart = artifact.get(CHART_ARTIFACT_KEY)
    if not isinstance(chart, dict) or not isinstance(chart.get("figure"), str):
        return None
    return chart["figure"], str(chart.get("label") or "")


# ── What the model is told about a chart ────────────────────────────────────

def _plain(value: Any) -> str:
    """A title or name from the figure, on one line and bounded, quoted for the summary."""
    if isinstance(value, dict):
        value = value.get("text")
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) > _NAME_CHARS:
        text = text[: _NAME_CHARS - 1] + "…"
    return json.dumps(text, ensure_ascii=False) if text else ""


def _typed_array(value: dict) -> list[Any] | None:
    code = _TYPED_ARRAY_FORMATS.get(str(value.get("dtype") or ""))
    if code is None:
        return None
    try:
        data = base64.b64decode(str(value.get("bdata") or ""), validate=True)
    except (binascii.Error, ValueError):
        return None
    size = struct.calcsize("<" + code)
    count = len(data) // size
    return list(struct.unpack(f"<{count}{code}", data[: count * size]))


def _values(value: Any) -> list[Any] | None:
    """A trace's data array, flattened: JSON lists and Plotly's typed arrays."""
    if isinstance(value, dict) and "bdata" in value:
        return _typed_array(value)
    if not isinstance(value, list):
        return None
    flat: list[Any] = []
    for item in value:
        if isinstance(item, list):
            flat.extend(item)
        elif isinstance(item, dict) and "bdata" in item:
            flat.extend(_typed_array(item) or [])
        else:
            flat.append(item)
    return flat


def _number(value: float) -> str:
    if isinstance(value, float) and value.is_integer() and abs(value) < 1e15:
        value = int(value)
    return f"{value:,}" if isinstance(value, int) else f"{value:.4g}"


def _span(values: list[Any] | None) -> str:
    """The extent of an axis: a numeric range, or its first and last categories. Gaps are skipped."""
    present = [
        value for value in values or []
        if value is not None and not (isinstance(value, float) and not math.isfinite(value))
    ]
    if not present:
        return ""
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in present):
        low, high = min(present), max(present)
        return _number(low) if low == high else f"{_number(low)} to {_number(high)}"
    first, last = _plain(present[0]), _plain(present[-1])
    return first if len(present) == 1 or first == last else f"{first} … {last}"


def _kind(trace: dict) -> str:
    kind = str(trace.get("type") or "scatter")
    if kind == "bar" and trace.get("orientation") == "h":
        return "horizontal bar"
    if kind in {"scatter", "scattergl"}:
        if trace.get("fill") not in (None, "", "none") or trace.get("stackgroup"):
            return "area"
        return "line" if "lines" in str(trace.get("mode") or "") else "scatter"
    if kind == "pie":
        try:
            return "donut" if float(trace.get("hole") or 0) > 0 else "pie"
        except (TypeError, ValueError):
            return "pie"
    return kind


def _shape(value: Any) -> str:
    if isinstance(value, dict) and value.get("shape"):
        return "×".join(part.strip() for part in str(value["shape"]).split(","))
    if isinstance(value, list) and value and isinstance(value[0], list):
        return f"{len(value)}×{len(value[0])}"
    return ""


def _count(number: int, noun: str) -> str:
    return f"{number:,} {noun}" + ("" if number == 1 else "s")


def _trace(trace: dict) -> str:
    kind = _kind(trace)
    name = _plain(trace.get("name"))
    parts = [f"{name} {kind}" if name else kind]
    if kind in {"pie", "donut"}:
        labels, values = _values(trace.get("labels")), _values(trace.get("values"))
        parts.append(_count(len(labels or values or []), "slice"))
        numbers = [v for v in values or [] if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if numbers:
            parts.append(f"total {_number(sum(numbers))}")
    elif kind == "heatmap":
        shape = _shape(trace.get("z"))
        if shape:
            parts.append(f"{shape} cells")
        span = _span(_values(trace.get("z")))
        if span:
            parts.append(f"values {span}")
    else:
        axes = {axis: _values(trace.get(axis)) for axis in ("x", "y")}
        count = max((len(values) for values in axes.values() if values), default=0)
        parts.append(_count(count, "value" if kind == "histogram" else "point"))
        parts.extend(f"{axis} {span}" for axis, values in axes.items() if (span := _span(values)))
    return ", ".join(parts)


def chart_summary(figure: dict, *, label: str, handle: str | None, size: int) -> str:
    """What the model reads instead of a chart's figure: enough to answer about it and refer to it."""
    traces = [trace for trace in figure.get("data") or [] if isinstance(trace, dict)]
    layout = figure.get("layout") if isinstance(figure.get("layout"), dict) else {}
    kinds = list(dict.fromkeys(_kind(trace) for trace in traces))
    title = _plain(layout.get("title"))
    lines = [label] if label else []
    chart = f"{' and '.join(kinds) or 'empty'} chart" + (f" {title}" if title else "")
    lines.append(f"Chart {handle}: shown to the person as an interactive {chart}." if handle
                 else f"Shown to the person as an interactive {chart}.")
    axes = []
    for axis in ("xaxis", "yaxis"):
        settings = layout.get(axis)
        axis_title = _plain(settings.get("title")) if isinstance(settings, dict) else ""
        if axis_title:
            axes.append(f"{axis[0]} {axis_title}")
    if axes:
        lines.append("Axes: " + ", ".join(axes) + ".")
    if traces:
        listed: list[str] = []
        used = 0
        for trace in traces[:_LISTED_TRACES]:
            line = _trace(trace)
            if listed and used + len(line) > _TRACES_CHARS:
                break
            listed.append(line)
            used += len(line) + 2
        more = len(traces) - len(listed)
        lines.append(f"Traces ({len(traces):,}): " + "; ".join(listed) + (f"; and {more:,} more." if more else "."))
    figure_note = f"The Plotly figure ({size:,} characters) is not repeated here"
    lines.append(f'{figure_note}; read_tool_output(handle="{handle}") returns it.' if handle else f"{figure_note}.")
    return "\n".join(lines)


# ── Moving a payload out of a new result ────────────────────────────────────

def _move_chart(message: Any, content: str, *, source: str, thread_id: str | None) -> Any:
    raw, label = split_marker(content, CHART_MARKER) or ("", "")
    try:
        figure = json.loads(raw)
    except ValueError:
        return message  # Not a figure: the client shows "unavailable", and the model gets the text.
    artifact = getattr(message, "artifact", None)
    if not isinstance(figure, dict) or not isinstance(figure.get("data"), list) or not isinstance(
            artifact, (dict, type(None))):
        return message
    from row_bot.tool_outputs import is_external_tool, spill

    handle = spill(raw, thread_id=thread_id, source=source, external=is_external_tool(source))
    summary = chart_summary(figure, label=label, handle=handle, size=len(raw))
    chart = {"figure": raw, "label": label[:LABEL_CHARS]}
    return message.model_copy(update={"content": summary, "artifact": {**(artifact or {}), CHART_ARTIFACT_KEY: chart}})


def _image_problem(encoded: str) -> str:
    """Why an image can't be shown, as the public media error code; empty when it can.

    The person's client shows PNG and JPEG images within the attachment size limit.
    """
    from row_bot.application.attachments import MAX_ATTACHMENT_BYTES

    if len(encoded) > 4 * ((MAX_ATTACHMENT_BYTES + 2) // 3):
        return "payload_too_large"
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        return "media_unavailable"
    if len(data) > MAX_ATTACHMENT_BYTES:
        return "payload_too_large"
    return "" if data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")) else "media_unavailable"


def _move_image(message: Any, content: str) -> Any:
    encoded, caption = split_marker(content, IMAGE_MARKER) or ("", "")
    encoded = encoded.strip()
    from row_bot.application.attachment_context import current_caches

    # Checked before the model is told anything, so its text says what the person sees.
    problem = _image_problem(encoded)
    update: dict[str, Any] = {}
    if problem:
        # The person sees the step's media error, as for a generated image that failed.
        update["additional_kwargs"] = {**(getattr(message, "additional_kwargs", None) or {}),
                                       "platform_media_error": problem}
        reason = ("it is larger than the attachment limit" if problem == "payload_too_large"
                  else "its data is not a valid PNG or JPEG image")
        note = f"[The image could not be shown: {reason}.]"
    else:
        caches = current_caches()
        shown = caches is not None and caches.pending_image is None
        if shown:
            # Published with the call's media, as a generated image is (agent._wrap_platform_tool_call).
            caches.pending_image = encoded
        amount = f"about {max(1, round(len(encoded) * 3 // 4 / 1024)):,} KB"
        note = (f"[Image shown to the person ({amount}); its data is not repeated here.]" if shown
                else f"[Image produced ({amount}); its data is not included here.]")
    update["content"] = f"{caption}\n{note}" if caption else note
    return message.model_copy(update=update)


def move_rendered_payload(message: Any, *, source: str = "", thread_id: str | None = None) -> Any:
    """Move a freshly created result's client payload out of the model's text.

    *source* is the tool that ran; the saved figure's read-back is fenced as its results are.
    Anything else is returned unchanged.
    """
    content = getattr(message, "content", None)
    if not isinstance(content, str):
        return message
    try:
        if content.startswith(CHART_MARKER):
            return _move_chart(message, content, source=source, thread_id=thread_id)
        if content.startswith(IMAGE_MARKER):
            return _move_image(message, content)
    except Exception:
        logger.warning("Could not move a rendered result out of the model's text", exc_info=True)
    return message
