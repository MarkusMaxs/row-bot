"""Only the person chooses who their commits are from.

On a Mac with no Git name and email, the agent set a repo-local "Row-Bot <row-bot@local>" without
asking. Setting Git's identity now always asks the person (the Auto reviewer never accepts it for
them), and a commit that has no identity says to ask rather than failing with an exit status.
These are approval tests: they stay in the pull request lane.
"""
from __future__ import annotations

import subprocess

import pytest

from row_bot.developer.runtime import sets_git_identity

pytestmark = pytest.mark.platform


@pytest.mark.parametrize("command", [
    'git config user.email "row-bot@local"',
    "git config --local user.name Row-Bot",
    "git config --global user.email me@example.com",
    "git config --unset user.name",
    'git -c user.name="Row-Bot" -c user.email=bot@local commit -m fix',
    'GIT_AUTHOR_NAME=Row-Bot git commit -m "fix"',
    '$env:GIT_COMMITTER_EMAIL="bot@local"; git commit -m fix',
])
def test_commands_that_choose_who_a_commit_is_from(command):
    assert sets_git_identity(command)


@pytest.mark.parametrize("command", [
    "git config --get user.email",
    "git config user.name",
    "git config --list",
    "git config core.autocrlf false",
    'git commit -m "Set user.name in the docs"',
    "git status",
])
def test_commands_that_only_read_it_or_do_something_else(command):
    assert not sets_git_identity(command)


def _shell(tmp_path, reload_for_data_dir):
    return tuple(reload_for_data_dir(tmp_path / "data", "row_bot.tools.approval_gate", "row_bot.tools.shell_tool"))


def test_under_auto_setting_git_identity_asks_the_person_not_the_reviewer(tmp_path, monkeypatch, reload_for_data_dir):
    from row_bot.approval_review import ReviewVerdict

    approval_gate, shell_tool = _shell(tmp_path, reload_for_data_dir)
    monkeypatch.setattr(approval_gate, "current_approval_mode", lambda: "auto")
    reviewed: list = []
    monkeypatch.setattr("row_bot.approval_review.review",
                        lambda payload, **_kw: reviewed.append(payload) or ReviewVerdict("approve", "You asked to commit."))
    asked: list[dict] = []
    monkeypatch.setattr("langgraph.types.interrupt", lambda payload: asked.append(payload) or False)

    output = shell_tool.ShellTool().execute('git config user.email "row-bot@local"', approval_reason="To commit.")

    assert reviewed == []
    assert [payload["args"]["command"] for payload in asked] == ['git config user.email "row-bot@local"']
    assert asked[0]["review"]["reason"] == "Only you can choose the name and email your commits are from."
    assert "did not run" in output


def test_a_commit_with_no_name_and_email_says_to_ask_the_person(tmp_path, monkeypatch):
    from row_bot.developer.git import NO_GIT_IDENTITY, commit_changes

    config = tmp_path / "gitconfig"
    # No name or email anywhere, and no guessing one from the computer's user and host name.
    config.write_text("[user]\n\tuseConfigOnly = true\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME",
                 "GIT_COMMITTER_EMAIL", "EMAIL"):
        monkeypatch.delenv(name, raising=False)
    repo = tmp_path / "shop"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "app.py").write_text("print('hello')\n", encoding="utf-8")

    with pytest.raises(ValueError) as refused:
        commit_changes(str(repo), "Fix the date bug")

    assert str(refused.value) == NO_GIT_IDENTITY
    # Nothing was set on the person's behalf.
    assert subprocess.run(["git", "-C", str(repo), "config", "user.email"], capture_output=True).returncode != 0
