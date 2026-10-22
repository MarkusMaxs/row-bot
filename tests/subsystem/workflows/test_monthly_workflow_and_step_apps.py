"""B345: a monthly workflow keeps its schedule as Monthly, and its prompt step names the apps it uses."""
from __future__ import annotations

from datetime import datetime

import pytest

from row_bot.integrations import scope
from tests.fixtures.tasks import fresh_tasks_module

pytestmark = pytest.mark.subsystem


def _app(item_id: str, name: str, *, readiness: str = "ready") -> dict:
    return {"id": item_id, "kind": "mcp", "server": name, "name": name, "app": {"id": name.lower(), "name": name},
            "icon": "letter:" + name[0], "lifecycle": "installed", "readiness": readiness, "parent_id": None,
            "children": [], "blockers": []}


@pytest.fixture
def tool(tmp_path, monkeypatch, reload_for_data_dir):
    tasks = fresh_tasks_module(tmp_path, reload_for_data_dir)
    from row_bot.tools import task_tool

    monkeypatch.setattr(task_tool, "tasks_db", tasks)
    apps = [_app("mcp:stripe", "Stripe"), _app("mcp:notion", "Notion"),
            _app("mcp:sentry", "Sentry", readiness="needs_sign_in")]
    monkeypatch.setattr(scope, "_items", lambda strict=False: apps)
    return tasks, task_tool


def _created_id(reply: str) -> str:
    return reply.split("ID: ")[1].split()[0]


def test_a_monthly_schedule_is_kept_as_written_and_fires_on_that_day(tool):
    tasks, task_tool = tool
    reply = task_tool._task_create(name="Month-end close", prompts=["Close the month."], schedule="cron:0 8 1 * *")
    task = tasks.get_task(_created_id(reply))
    # Saved exactly as the client's Monthly preset writes and reads it: "Monthly on the 1st at 08:00".
    assert task["schedule"] == "cron:0 8 1 * *"
    fire = tasks._build_trigger(task).get_next_fire_time(None, datetime(2026, 10, 21, 12, 0).astimezone())
    assert (fire.day, fire.hour, fire.minute) == (1, 8, 0)
    assert fire.month == 11


def test_a_prompt_step_keeps_the_apps_it_names(tool):
    tasks, task_tool = tool
    reply = task_tool._task_create(name="Month-end close", schedule="cron:0 8 1 * *", steps=[
        {"type": "prompt", "prompt": "Total last month's Stripe payouts and log them in Notion.",
         "apps": ["Stripe", "@notion"]}])
    task = tasks.get_task(_created_id(reply))
    assert task["steps"][0]["apps"] == ["mcp:stripe", "mcp:notion"]


def test_an_app_that_is_not_set_up_is_refused_with_the_apps_that_are(tool):
    tasks, task_tool = tool
    reply = task_tool._task_create(name="Close", steps=[
        {"type": "prompt", "prompt": "Check the books.", "apps": ["Xero"]}])
    assert reply.startswith("Error creating task:")
    assert "Xero" in reply and "Notion, Stripe" in reply
    assert not tasks.list_tasks()
