from __future__ import annotations

from pathlib import Path


def fresh_tasks_module(tmp_path: Path, reload_for_data_dir):
    (tasks,) = reload_for_data_dir(tmp_path / "row-bot-data", "row_bot.tasks")
    return tasks


def sample_workflow_steps() -> list[dict]:
    return [
        {"type": "prompt", "id": "draft", "prompt": "Draft a summary", "next": "approval"},
        {"type": "approval", "id": "approval", "message": "Approve summary?", "if_approved": "notify", "if_denied": "end"},
        {"type": "notify", "id": "notify", "channel": "fake", "message": "Done"},
    ]
