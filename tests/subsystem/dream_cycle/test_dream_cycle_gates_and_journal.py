from __future__ import annotations

from datetime import datetime, timedelta, timezone

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


def test_config_defaults_persist_and_invalid_windows_are_rejected(tmp_path, monkeypatch, reload_for_data_dir) -> None:
    dream_cycle = fresh_dream_cycle(tmp_path, monkeypatch, reload_for_data_dir)

    assert dream_cycle.get_config()["enabled"] is True
    dream_cycle.set_enabled(False)
    dream_cycle.set_window(22, 2)

    assert dream_cycle.is_enabled() is False
    assert dream_cycle.get_config()["window_start"] == 22
    assert dream_cycle.get_config()["window_end"] == 2

    for start, end in [(-1, 2), (1, 24), (5, 5)]:
        with pytest.raises(ValueError):
            dream_cycle.set_window(start, end)

    dream_cycle._CONFIG_FILE.write_text("{not-json", encoding="utf-8")
    assert dream_cycle.get_config()["enabled"] is True


def test_journal_is_append_only_capped_and_status_reports_last_run(tmp_path, monkeypatch, reload_for_data_dir) -> None:
    dream_cycle = fresh_dream_cycle(tmp_path, monkeypatch, reload_for_data_dir)

    for idx in range(105):
        dream_cycle._append_journal(
            {
                "cycle_id": f"cycle-{idx}",
                "timestamp": f"2026-06-23T00:{idx % 60:02d}:00+00:00",
                "summary": f"summary {idx}",
            }
        )

    journal = dream_cycle._load_journal()
    status = dream_cycle.get_dream_status()

    assert len(journal) == 100
    assert journal[0]["cycle_id"] == "cycle-5"
    assert dream_cycle.get_journal(limit=2)[0]["cycle_id"] == "cycle-103"
    assert status["last_run"] == "2026-06-23T00:44:00+00:00"
    assert status["last_summary"] == "summary 104"


def test_rejection_cache_keys_unordered_pairs_on_their_evidence_and_expires(tmp_path, monkeypatch, reload_for_data_dir) -> None:
    dream_cycle = fresh_dream_cycle(tmp_path, monkeypatch, reload_for_data_dir)
    pair = dream_cycle._pair_key("b", "a")

    dream_cycle._record_rejection("infer", pair, "prompt with the evidence")

    assert dream_cycle._rejected_with_same_evidence("infer", dream_cycle._pair_key("a", "b"), "prompt with the evidence")
    assert not dream_cycle._rejected_with_same_evidence("infer", pair, "prompt with new evidence")
    assert not dream_cycle._rejected_with_same_evidence("merge", pair, "prompt with the evidence")

    cache = dream_cycle._load_rejection_cache()
    cache["infer"][pair]["at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    dream_cycle._save_rejection_cache(cache)
    assert not dream_cycle._rejected_with_same_evidence("infer", pair, "prompt with the evidence")


def test_time_only_rejections_from_before_are_asked_again(tmp_path, monkeypatch, reload_for_data_dir) -> None:
    dream_cycle = fresh_dream_cycle(tmp_path, monkeypatch, reload_for_data_dir)
    dream_cycle._REJECTION_CACHE_FILE.write_text(
        '{"a|b": "%s"}' % datetime.now(timezone.utc).isoformat(), encoding="utf-8")

    assert dream_cycle._load_rejection_cache()["infer"] == {}
    assert not dream_cycle._rejected_with_same_evidence("infer", "a|b", "any prompt")


def test_window_and_today_checks_use_persisted_journal(tmp_path, monkeypatch, reload_for_data_dir) -> None:
    dream_cycle = fresh_dream_cycle(tmp_path, monkeypatch, reload_for_data_dir)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return cls(2026, 6, 23, 2, 30)
            return cls(2026, 6, 23, 2, 30, tzinfo=tz)

    monkeypatch.setattr(dream_cycle, "datetime", FixedDateTime)
    dream_cycle.set_window(1, 5)
    assert dream_cycle._in_dream_window() is True

    dream_cycle.set_window(22, 3)
    assert dream_cycle._in_dream_window() is True

    dream_cycle._save_journal([{"timestamp": "2026-06-23T01:00:00+00:00"}])
    assert dream_cycle._already_ran_today() is True

    dream_cycle._save_journal([{"timestamp": "2026-06-22T01:00:00+00:00"}])
    assert dream_cycle._already_ran_today() is False


def test_should_dream_rejects_each_gate_and_accepts_clean_path(tmp_path, monkeypatch, reload_for_data_dir) -> None:
    from row_bot.runtime import executions

    dream_cycle = fresh_dream_cycle(tmp_path, monkeypatch, reload_for_data_dir)
    registry = executions.GenerationRuntimeRegistry()
    monkeypatch.setattr(executions, "generation_registry", registry)

    gates = {
        "enabled": True,
        "today": False,
        "window": True,
        "idle": True,
    }
    monkeypatch.setattr(dream_cycle, "is_enabled", lambda: gates["enabled"])
    monkeypatch.setattr(dream_cycle, "_already_ran_today", lambda: gates["today"])
    monkeypatch.setattr(dream_cycle, "_in_dream_window", lambda: gates["window"])
    monkeypatch.setattr(dream_cycle, "_is_idle", lambda: gates["idle"])

    assert dream_cycle._should_dream() is True

    for key, value in [
        ("enabled", False),
        ("today", True),
        ("window", False),
        ("idle", False),
    ]:
        gates[key] = value
        assert dream_cycle._should_dream() is False
        gates[key] = {"enabled": True, "today": False, "window": True, "idle": True}[key]

    turn = registry.register("conversation-a")
    assert dream_cycle._should_dream() is False
    registry.finish(turn)
    assert dream_cycle._should_dream() is True


def test_a_loaded_but_idle_local_model_does_not_hold_dream_back(tmp_path, monkeypatch, reload_for_data_dir) -> None:
    """F48: Ollama keeps a model loaded for its keep-alive after every reply, and ``/api/ps``
    has no count of requests in flight, so a loaded model was read as "busy" and Dream never
    ran while one was resident. Row-Bot's own idle and generation state decide instead."""
    import json
    import urllib.request

    from row_bot.runtime import executions

    dream_cycle = fresh_dream_cycle(tmp_path, monkeypatch, reload_for_data_dir)
    monkeypatch.setattr(executions, "generation_registry", executions.GenerationRuntimeRegistry())
    for gate in ("is_enabled", "_in_dream_window", "_is_idle"):
        monkeypatch.setattr(dream_cycle, gate, lambda: True)
    monkeypatch.setattr(dream_cycle, "_already_ran_today", lambda: False)

    resident = {"models": [{
        "name": "fixture-model:latest", "model": "fixture-model:latest", "size": 5_137_025_024,
        "digest": "0" * 64, "details": {"format": "gguf"}, "expires_at": "2026-10-11T02:30:00Z",
        "size_vram": 5_137_025_024,
    }]}
    asked: list[str] = []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return json.dumps(resident).encode()

    def urlopen(request, *_args, **_kwargs):
        asked.append(getattr(request, "full_url", str(request)))
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    assert dream_cycle._should_dream() is True
    assert asked == []
