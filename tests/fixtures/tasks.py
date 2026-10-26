from __future__ import annotations

from pathlib import Path


def fresh_tasks_module(tmp_path: Path, reload_for_data_dir):
    (tasks,) = reload_for_data_dir(tmp_path / "row-bot-data", "row_bot.tasks")
    return tasks

