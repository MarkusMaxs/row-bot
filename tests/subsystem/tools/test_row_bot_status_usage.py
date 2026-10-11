"""row_bot_status category='usage': "how many tokens did I use?" from the local ledger."""
from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.subsystem


def _record(**overrides) -> None:
    from row_bot.prompt_cache import ModelUsage
    from row_bot.providers.usage_ledger import UsageRecord, record_model_usage

    values = {
        "purpose": "main",
        "provider": "anthropic",
        "model": "claude-sonnet-4-5",
        "usage": ModelUsage(input_tokens=10_000, output_tokens=500, cache_read_tokens=9_000, cache_write_tokens=500),
        "thread_id": "thread-now",
    }
    values.update(overrides)
    assert record_model_usage(UsageRecord(**values))


def test_usage_reports_this_conversation_recent_windows_models_and_purposes(tmp_path, monkeypatch):
    from row_bot.prompt_cache import ModelUsage
    from row_bot.tools import row_bot_status_tool as status

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(status, "_active_runtime_thread_id", lambda: "thread-now")
    now = time.time()
    _record()
    _record(purpose="compaction", usage=ModelUsage(input_tokens=2_000, output_tokens=300, cache_read_tokens=0))
    _record(provider="openrouter", model="openai/gpt-4.1", thread_id="thread-other", purpose="chat_only",
            usage=ModelUsage(input_tokens=1_000, output_tokens=100, cache_read_tokens=500, cost=0.0123),
            created_at=now - 3 * 86_400)
    _record(provider="ollama", model="qwen3:14b", thread_id="", purpose="dream",
            usage=ModelUsage(), created_at=now - 20 * 86_400)

    report = status._row_bot_status("usage")

    lines = report.splitlines()
    assert lines[0].startswith("**Model Usage**")
    conversation = next(line for line in lines if line.startswith("- This conversation:"))
    assert "2 calls" in conversation and "12,800 tokens (12,000 in, 800 out)" in conversation
    assert "75% of input read from cache" in conversation
    day = next(line for line in lines if line.startswith("- Last 24 hours:"))
    week = next(line for line in lines if line.startswith("- Last 7 days:"))
    month = next(line for line in lines if line.startswith("- Last 30 days:"))
    assert "2 calls" in day and "3 calls" in week and "4 calls" in month
    assert "$0.0123 reported by the provider" in week
    assert "1 without reported counts" in month
    assert "  - anthropic / claude-sonnet-4-5: 2 calls" in report
    assert "  - openrouter / openai/gpt-4.1: 1 call ·" in report
    assert "  - compaction: 1 call" in report and "  - dream: 1 call" in report
    assert "no price table" in report


def test_usage_says_plainly_when_nothing_was_recorded(tmp_path, monkeypatch):
    from row_bot.tools import row_bot_status_tool as status

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(status, "_active_runtime_thread_id", lambda: "")

    report = status._row_bot_status("usage")

    assert "- Last 7 days: no model calls recorded" in report
    assert "This conversation" not in report
    assert not (tmp_path / "usage.db").exists()
