"""row_bot_status category='features': the feature list moved out of every chat's prompt."""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.subsystem


def test_features_lists_what_row_bot_does_and_how_to_set_it_up(tmp_path, monkeypatch):
    from row_bot.tools import row_bot_status_tool as status

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    text = status._row_bot_status("features")

    assert text.startswith("**Row-Bot features**")
    assert "- Web Search:" in text and "Setup: Settings" in text
    assert "- Task Automation:" in text


def test_every_chat_gets_a_short_self_description_that_points_to_features(tmp_path, monkeypatch):
    from row_bot import self_knowledge

    monkeypatch.setattr("row_bot.identity.is_self_improvement_enabled", lambda: False)
    block = self_knowledge.build_static_self_knowledge_block()

    assert "row_bot_status with category\n'features'" in block
    # No feature list and no fixed folder path: the configured workspace may be anywhere.
    assert "Core capabilities:" not in block
    assert "~/Documents" not in block
    assert len(block) < 600
