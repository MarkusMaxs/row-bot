"""Goals stop by themselves only for the person to decide (decision D4, WP C3).

- Goals that were working when Row-Bot closed come back paused, with one notice.
- Two failed progress checks in a row pause the goal and say so.
- A goal's own token budget pauses it when used up; Resume gives it the budget again.
- Turns stay unlimited unless someone set a limit.

The verifier is scripted (``judge``), so no model is called.
"""
from __future__ import annotations

import pytest

from tests.fixtures.goals import goal_modules, judge

# Start-up goal handling runs on Windows and macOS too (PLAN test strategy).
pytestmark = [pytest.mark.subsystem, pytest.mark.platform]


@pytest.fixture
def goals(tmp_path, reload_for_data_dir):
    return goal_modules(tmp_path, reload_for_data_dir)


@pytest.fixture
def notices(monkeypatch):
    from row_bot.application import app_notices

    journal = app_notices.NoticeJournal()
    monkeypatch.setattr(app_notices, "app_notices", journal)
    return journal


@pytest.fixture
def notified(monkeypatch):
    from row_bot import notifications

    sent: list[dict] = []
    monkeypatch.setattr(
        notifications, "notify",
        lambda title, message, *_args, **kwargs: sent.append({"title": title, "message": message, **kwargs}),
    )
    return sent


def _set_token_budget(goals, goal_id: str, budget: int) -> None:
    conn = goals._get_conn()
    try:
        conn.execute("UPDATE thread_goals SET token_budget = ? WHERE id = ?", (budget, goal_id))
        conn.commit()
    finally:
        conn.close()


def _failing_check(goals, thread: str, turn: int):
    def verifier(_goal, _context):
        raise RuntimeError("provider unavailable")

    return goals.after_turn(thread_id=thread, turn_id=f"turn-{turn}", verifier=verifier)


def test_goals_working_when_row_bot_closed_come_back_paused_with_one_notice(goals, notices):
    from row_bot import tasks
    from row_bot.application import conversation_followups

    first = goals.start_goal("thread-a", "Write the release notes")
    second = goals.start_goal("thread-b", "Tidy the photo library")
    on_channel = goals.start_goal("thread-c", "Answer on the channel")
    tasks.record_thread_channel_ref("thread-c", channel="sms", target="fixture-chat")
    waiting = goals.start_goal("thread-d", "Send the invoice")
    goals.set_goal_status(waiting["id"], "waiting_approval", reason="Waiting for approval.", verdict="paused")
    already = goals.start_goal("thread-e", "Plan the trip")
    goals.pause_goal("thread-e")

    assert goals.settle_interrupted_goals() == 3

    for goal in (first, second):
        current = goals.get_goal(goal["id"])
        assert (current["status"], current["last_reason"]) == (
            "paused", "Paused when Row-Bot closed. Resume to continue.")
    assert goals.get_goal(on_channel["id"])["status"] == "paused"
    assert goals.get_goal(waiting["id"])["status"] == "waiting_approval"
    assert goals.get_goal(already["id"])["last_reason"] == "Paused by user."
    shown = notices.since(0)
    assert [(notice.title, notice.message, notice.requested) for notice in shown] == [
        ("Goals paused", "3 goals paused when Row-Bot closed. Resume from their conversations to continue.", True),
    ]
    # Nothing goes on by itself after a normal launch.
    assert conversation_followups.continue_goals_after_restart(object()) == 0
    # A second pass (nothing left working) adds no notice.
    assert goals.settle_interrupted_goals() == 0
    assert len(notices.since(0)) == 1


def test_resume_after_start_up_lets_the_goal_go_on(goals, notices):
    goal = goals.start_goal("thread-a", "Write the release notes")
    goals.settle_interrupted_goals()

    resumed = goals.resume_goal("thread-a")

    assert resumed["status"] == "active"
    assert judge(goals, "thread-a", 1, progress="progress", reason="Drafted the intro.").should_continue
    assert goals.get_goal(goal["id"])["turns_used"] == 1


def test_a_restart_row_bot_caused_lets_conversation_goals_go_on(goals, notices, monkeypatch):
    from row_bot import tasks
    from row_bot.application import conversation_followups, startup_state

    monkeypatch.setattr(startup_state, "last_exit_was_app_initiated", lambda: True)
    goal = goals.start_goal("thread-a", "Write the release notes")
    on_channel = goals.start_goal("thread-c", "Answer on the channel")
    tasks.record_thread_channel_ref("thread-c", channel="sms", target="fixture-chat")
    started = []
    monkeypatch.setattr(
        conversation_followups, "start_goal_turn",
        lambda _service, conversation_id, _goal, *, initial: started.append((conversation_id, initial)),
    )

    assert goals.settle_interrupted_goals() == 1
    assert goals.get_goal(goal["id"])["status"] == "active"
    assert goals.get_goal(on_channel["id"])["status"] == "paused"
    assert [notice.message for notice in notices.since(0)] == [
        "1 goal paused when Row-Bot closed. Resume from its conversation to continue."]
    assert conversation_followups.continue_goals_after_restart(object()) == 1
    assert started == [("thread-a", False)]


def test_two_failed_progress_checks_in_a_row_pause_the_goal_and_say_so(goals, notified):
    goal = goals.start_goal("thread", "Migrate the notes")

    first = _failing_check(goals, "thread", 1)
    assert first.should_continue is True
    assert notified == []

    second = _failing_check(goals, "thread", 2)

    current = goals.get_goal(goal["id"])
    assert second.should_continue is False
    assert current["status"] == "paused"
    assert "couldn't check this goal's progress 2 times in a row" in current["last_reason"]
    assert [(item["title"], item["toast_type"], item["requested"]) for item in notified] == [
        ("Goal paused", "warning", True)]
    assert "Migrate the notes" in notified[0]["message"]


def test_a_working_progress_check_or_resume_starts_the_count_again(goals, notified):
    goal = goals.start_goal("thread", "Migrate the notes")

    assert _failing_check(goals, "thread", 1).should_continue
    assert judge(goals, "thread", 2, progress="progress", reason="Moved 10 notes.").should_continue
    assert goals.get_goal(goal["id"])["verifier_failures"] == 0
    assert _failing_check(goals, "thread", 3).should_continue

    assert not _failing_check(goals, "thread", 4).should_continue
    goals.resume_goal("thread")
    assert goals.get_goal(goal["id"])["verifier_failures"] == 0
    assert _failing_check(goals, "thread", 5).should_continue
    assert len(notified) == 1


def test_a_goal_pauses_when_its_token_budget_is_used_and_resume_gives_it_another(goals, notified):
    goal = goals.start_goal("thread", "Summarise the inbox")
    _set_token_budget(goals, goal["id"], 1_000)

    goals.record_goal_tokens(goal["id"], 600, "reply-1")
    assert judge(goals, "thread", 1, progress="progress").should_continue
    goals.record_goal_tokens(goal["id"], 500, "reply-2")
    decision = judge(goals, "thread", 2, progress="progress")

    current = goals.get_goal(goal["id"])
    assert decision.should_continue is False
    assert (current["status"], current["last_reason"]) == (
        "paused", "Used its budget of 1,000 tokens. Resume to keep going.")
    assert [item["title"] for item in notified] == ["Goal paused"]
    assert "used its budget of 1,000 tokens" in notified[0]["message"]

    goals.resume_goal("thread")
    assert judge(goals, "thread", 3, progress="progress").should_continue
    goals.record_goal_tokens(goal["id"], 1_000, "reply-3")
    assert not judge(goals, "thread", 4, progress="progress").should_continue


def test_without_a_budget_or_turn_limit_a_goal_keeps_going(goals, notified):
    goal = goals.start_goal("thread", "Keep the wiki tidy", max_turns=0)

    for turn in range(1, 6):
        goals.record_goal_tokens(goal["id"], 250_000, f"reply-{turn}")
        assert judge(goals, "thread", turn, progress="progress").should_continue

    assert goals.get_goal(goal["id"])["status"] == "active"
    assert notified == []
