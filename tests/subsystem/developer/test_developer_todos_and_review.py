from __future__ import annotations

import subprocess


def _fresh_modules(tmp_path, reload_for_data_dir):

    return tuple(reload_for_data_dir(tmp_path / "data", "row_bot.developer.todos", "row_bot.developer.review", "row_bot.developer.storage"))


def _git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _init_repo(repo):
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True, text=True)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("# Test\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def test_developer_todos_persist_by_thread(tmp_path, reload_for_data_dir):
    todos, _review, _storage = _fresh_modules(tmp_path, reload_for_data_dir)

    created = todos.replace_todos_from_labels("thread-1", ["Scan repo", "Run tests"])
    assert len(created) == 2
    assert todos.list_todos("thread-1")[0].label == "Scan repo"

    updated = todos.set_todo_status("thread-1", created[0].id, "in_progress")
    assert updated[0].status == "in_progress"
    assert todos.list_todos("missing") == []


def test_developer_review_lists_changes_and_diff(tmp_path, reload_for_data_dir):
    _todos, review, _storage = _fresh_modules(tmp_path, reload_for_data_dir)
    repo = tmp_path / "repo"
    _init_repo(repo)

    (repo / "README.md").write_text("# Test\n\nChanged\n", encoding="utf-8")
    (repo / "new.txt").write_text("new\n", encoding="utf-8")

    changed = review.list_changed_files(str(repo))
    paths = {row.path for row in changed}
    assert "README.md" in paths
    assert "new.txt" in paths

    diff = review.get_file_diff(str(repo), "README.md")
    assert "+Changed" in diff

    untracked_diff = review.get_file_diff(str(repo), "new.txt")
    assert "new file mode" in untracked_diff
    assert "+new" in untracked_diff

    stats = review.get_workspace_diff_stats(str(repo))
    assert stats.files == 2
    assert stats.additions >= 2
