from __future__ import annotations


import pytest


pytestmark = pytest.mark.subsystem


@pytest.fixture(autouse=True)
def _chosen_model(monkeypatch):
    """Nothing is preset (decision 9): these flows run with a chosen model."""
    from row_bot import models

    monkeypatch.setattr(models, "_current_model", "model:ollama:fixture-model")


def fresh_dream_cycle(tmp_path, monkeypatch, reload_for_data_dir):

    reload_for_data_dir(tmp_path / "row-bot-data", "row_bot.knowledge_graph")
    return reload_for_data_dir(tmp_path / "row-bot-data", "row_bot.dream_cycle")[0]


def test_should_dream_requires_enabled_idle_window_and_no_turn_in_flight(tmp_path, monkeypatch, reload_for_data_dir) -> None:
    from row_bot.runtime import executions

    dream_cycle = fresh_dream_cycle(tmp_path, monkeypatch, reload_for_data_dir)
    registry = executions.GenerationRuntimeRegistry()
    monkeypatch.setattr(executions, "generation_registry", registry)

    monkeypatch.setattr(dream_cycle, "is_enabled", lambda: True)
    monkeypatch.setattr(dream_cycle, "_already_ran_today", lambda: False)
    monkeypatch.setattr(dream_cycle, "_in_dream_window", lambda: True)
    monkeypatch.setattr(dream_cycle, "_is_idle", lambda: True)

    assert dream_cycle._should_dream() is True

    monkeypatch.setattr(dream_cycle, "_is_idle", lambda: False)
    assert dream_cycle._should_dream() is False

    monkeypatch.setattr(dream_cycle, "_is_idle", lambda: True)
    turn = registry.register("conversation-a")
    assert dream_cycle._should_dream() is False
    registry.finish(turn)

    monkeypatch.setattr(dream_cycle, "_already_ran_today", lambda: True)
    assert dream_cycle._should_dream() is False


def test_run_dream_cycle_below_minimum_records_journal_skip(tmp_path, monkeypatch, reload_for_data_dir) -> None:
    dream_cycle = fresh_dream_cycle(tmp_path, monkeypatch, reload_for_data_dir)

    summary = dream_cycle.run_dream_cycle()
    journal = dream_cycle.get_journal(limit=1)

    assert summary["merges"] == []
    assert summary["enrichments"] == []
    assert summary["inferred_relations"] == []
    assert summary["summary"].startswith("Skipped — waits for 20 saved memories;")
    assert "entities" not in summary["summary"]
    assert journal[-1]["cycle_id"] == summary["cycle_id"]
