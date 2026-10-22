"""N9: a workflow's own code folder. Every step runs in it, runs say which folder they used, and a
folder that has been removed stops the run before it starts, saying so in plain words."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.subsystem


@pytest.fixture
def setup(tmp_path, monkeypatch, reload_for_data_dir):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    storage = importlib.import_module("row_bot.developer.storage")
    monkeypatch.setattr(storage, "DATA_DIR", data_dir)
    monkeypatch.setattr(storage, "DEVELOPER_DIR", data_dir / "developer")
    monkeypatch.setattr(storage, "WORKSPACES_PATH", data_dir / "developer" / "workspaces.json")
    tasks, threads = reload_for_data_dir(data_dir, "row_bot.tasks", "row_bot.threads")
    from row_bot.developer.state import DeveloperWorkspace

    folders = {}
    for name in ("row-bot-landing", "docs-site"):
        path = tmp_path / name
        path.mkdir()
        storage.save_workspace(DeveloperWorkspace(id=f"dev_{name.replace('-', '_')}", name=name, path=str(path)))
        folders[name] = (f"dev_{name.replace('-', '_')}", path)
    # A run's worker is not started here: only what happens before it starts is under test.
    from row_bot.runtime import executions

    started: list[str] = []
    monkeypatch.setattr(executions.generation_registry, "thread",
                        lambda **kwargs: SimpleNamespace(start=lambda: started.append(kwargs["domain_id"])))
    return SimpleNamespace(tasks=tasks, threads=threads, storage=storage, folders=folders, started=started)


def _workflow(tasks, **values) -> str:
    return tasks.create_task("Landing checks", steps=[{"type": "prompt", "prompt": "Run the checks"}], **values)


def _settings(task_id):
    from row_bot.application import task_settings_controls as controls

    return controls, controls.get_task_settings(task_id)


def _save(controls, snapshot, **changes):
    from dataclasses import replace

    return controls.update_saved_task_settings(
        snapshot.task_id, replace(snapshot.fields, **changes), expected_revision=snapshot.revision,
        expected_profile_revision=snapshot.profile_revision, validate=lambda: None,
    )


def _run(setup, task_id):
    tasks = setup.tasks
    thread_id = tasks._prepare_task_thread(tasks.get_task(task_id))
    tasks.run_task_background(task_id, thread_id, [])
    from row_bot.application.task_execution import list_task_runs

    return thread_id, list_task_runs(task_id).items[0]


def test_existing_workflows_keep_no_folder_and_run_in_their_conversation(setup):
    task_id = _workflow(setup.tasks)
    _controls, snapshot = _settings(task_id)
    assert snapshot.fields.workspace_id is None and snapshot.workspace_name is None

    thread_id, run = _run(setup, task_id)

    assert run.workspace_id is None and run.status == "running" and setup.started
    from row_bot import conversation_resources

    assert conversation_resources.list_bindings(thread_id).bindings == ()


def test_every_step_runs_in_the_workflow_folder_and_the_run_names_it(setup):
    task_id = _workflow(setup.tasks)
    controls, snapshot = _settings(task_id)
    folder_id, _path = setup.folders["row-bot-landing"]

    saved = _save(controls, snapshot, workspace_id=folder_id)

    assert (saved.fields.workspace_id, saved.workspace_name, saved.workspace_available) == (
        folder_id, "row-bot-landing", True)
    thread_id, run = _run(setup, task_id)
    assert (run.workspace_id, run.workspace_name) == (folder_id, "row-bot-landing")
    # The run's conversation works in the folder: every step, and a child agent that picks none.
    from row_bot import conversation_resources

    [binding] = conversation_resources.list_bindings(thread_id).bindings
    assert (binding.kind, binding.resource_id) == ("workspace", folder_id)
    assert setup.threads._get_thread_developer_workspace(thread_id) == folder_id


def test_a_kept_conversation_moves_to_the_workflows_folder(setup):
    task_id = _workflow(setup.tasks)
    controls, snapshot = _settings(task_id)
    landing, docs = setup.folders["row-bot-landing"][0], setup.folders["docs-site"][0]
    _save(controls, snapshot, workspace_id=docs, persistent_enabled=True)
    first, _run_one = _run(setup, task_id)
    controls, snapshot = _settings(task_id)
    _save(controls, snapshot, workspace_id=landing)

    second, _run_two = _run(setup, task_id)

    from row_bot import conversation_resources

    assert first == second
    assert [b.resource_id for b in conversation_resources.list_bindings(second).bindings] == [landing]


@pytest.mark.parametrize("removal", ["hidden", "deleted_on_disk", "forgotten"])
def test_a_removed_folder_stops_the_run_before_it_starts(setup, removal):
    task_id = _workflow(setup.tasks)
    controls, snapshot = _settings(task_id)
    folder_id, path = setup.folders["row-bot-landing"]
    _save(controls, snapshot, workspace_id=folder_id)
    if removal == "hidden":
        setup.storage.remove_workspace(folder_id)
    elif removal == "deleted_on_disk":
        path.rmdir()
    else:
        setup.storage.delete_workspace_record(folder_id)

    _thread_id, run = _run(setup, task_id)

    assert run.status == "blocked"
    assert run.notice == (
        "row-bot-landing isn't a code folder any more. Choose another in this workflow's settings."
    )
    assert setup.started == []
    _controls, after = _settings(task_id)
    assert (after.workspace_name, after.workspace_available) == ("row-bot-landing", False)


def test_a_removed_folder_cant_be_chosen(setup):
    task_id = _workflow(setup.tasks)
    controls, snapshot = _settings(task_id)
    folder_id, _path = setup.folders["docs-site"]
    setup.storage.remove_workspace(folder_id)

    with pytest.raises(controls.TaskSettingsError) as refused:
        _save(controls, snapshot, workspace_id=folder_id)

    assert refused.value.code == "task_settings_folder_unavailable"
    assert setup.tasks.get_task(task_id)["workspace_id"] == ""


def test_choosing_none_goes_back_to_the_conversations_folder(setup):
    task_id = _workflow(setup.tasks)
    controls, snapshot = _settings(task_id)
    snapshot = _save(controls, snapshot, workspace_id=setup.folders["docs-site"][0])

    cleared = _save(controls, snapshot, workspace_id=None)

    assert cleared.fields.workspace_id is None
    assert (setup.tasks.get_task(task_id)["workspace_id"], setup.tasks.get_task(task_id)["workspace_name"]) == ("", "")
