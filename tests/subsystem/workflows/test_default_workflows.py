from __future__ import annotations


import pytest


@pytest.fixture
def case_dir(tmp_path):
    path = tmp_path / "case"
    path.mkdir()
    return path


def test_default_workflows_are_switched_off_manual_and_one_message_each(reload_for_data_dir, case_dir):
    (tasks,) = reload_for_data_dir(case_dir, "row_bot.tasks")

    tasks.seed_default_tasks()
    seeded = tasks.list_tasks()

    assert len(seeded) == 5
    assert all(t["enabled"] is False and t.get("schedule") in ("", None) for t in seeded)
    assert all(len(t["prompts"]) == 1 and not t.get("notify_only") for t in seeded)
    # Each says what to fill in before it is switched on.
    joined = "\n".join(t["prompts"][0] for t in seeded)
    for placeholder in ("<topic>", "<topic-or-decision>", "<product-or-project>", "<market-or-customer-segment>"):
        assert placeholder in joined


def test_existing_users_are_not_reseeded_automatically(reload_for_data_dir, case_dir):
    data_dir = case_dir
    import row_bot.tasks as tasks

    (tasks,) = reload_for_data_dir(data_dir, "row_bot.tasks")
    tasks.create_task(name="User workflow", prompts=["Do something"], enabled=False)
    tasks.seed_default_tasks()

    names = [t["name"] for t in tasks.list_tasks()]

    assert names == ["User workflow"]
    assert (data_dir / ".tasks_seeded").exists()
