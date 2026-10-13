"""An MCP result's structured content is sent once: not again when its text block already is it (WP D1)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from row_bot.integrations import views
from row_bot.mcp_client.results import STRUCTURED_SAME_AS_TEXT, normalize_call_result


pytestmark = pytest.mark.subsystem

STRUCTURED = {"issues": [{"id": 7, "title": "Login fails"}, {"id": 9, "title": "Slow export"}], "total": 2}


def _result(*texts: str, structured=STRUCTURED, error: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(text=text) for text in texts],
        structuredContent=structured,
        isError=error,
    )


@pytest.mark.parametrize("text", [
    json.dumps(STRUCTURED),
    json.dumps(STRUCTURED, indent=2),
    json.dumps(STRUCTURED, separators=(",", ":"), sort_keys=True),
])
def test_text_that_already_is_the_structured_content_is_not_repeated(text):
    result = normalize_call_result(_result(text))

    assert result == f"{text}\n\n{STRUCTURED_SAME_AS_TEXT}"


def test_different_or_missing_text_keeps_the_structured_content_compact():
    summary = normalize_call_result(_result("Found 2 issues."))
    bare = normalize_call_result(_result())
    both = normalize_call_result(_result("Found 2 issues.", json.dumps(STRUCTURED)))

    for result in (summary, bare, both):
        assert result.endswith("STRUCTURED_CONTENT:\n" + json.dumps(STRUCTURED, separators=(",", ":")))
    assert summary.startswith("Found 2 issues.")


def test_an_error_with_the_same_text_is_still_marked_as_an_error():
    result = normalize_call_result(_result(json.dumps(STRUCTURED), error=True))

    assert result.startswith("MCP tool error: " + json.dumps(STRUCTURED))


@pytest.mark.parametrize("error", [False, True])
def test_an_apps_view_still_gets_the_structured_content_back(error):
    """An MCP app's view rebuilds its result from the chat's text form (integrations/views.py)."""
    text = json.dumps(STRUCTURED, indent=2)
    chat_text = "Approval: not needed\n" + normalize_call_result(_result(text, error=error))

    rebuilt = views._result(chat_text)

    assert rebuilt["structuredContent"] == STRUCTURED
    assert rebuilt["content"] == [{"type": "text", "text": text}]
    assert rebuilt.get("isError", False) is error
