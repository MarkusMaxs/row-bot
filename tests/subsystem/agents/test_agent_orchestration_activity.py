from __future__ import annotations


import pytest


pytestmark = pytest.mark.subsystem


def _fresh_modules(tmp_path, reload_for_data_dir):
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    tasks, _profiles, _settings, agent_runs, orchestrator = reload_for_data_dir(
        data_dir,
        "row_bot.tasks",
        "row_bot.agent_profiles",
        "row_bot.agent_settings",
        "row_bot.agent_runs",
        "row_bot.agent_orchestrator",
    )
    return tasks, agent_runs, orchestrator


def _orchestration(orchestrator, thread_id: str, generation: str):
    orchestration = orchestrator.create_or_get_orchestration(
        parent_thread_id=thread_id,
        parent_generation_id=generation,
        root_objective=f"Objective for {thread_id}",
        model_ref="provider:model",
        approval_mode="approve",
        runtime_surface="normal_chat",
        orchestration_version=2,
    )
    return orchestrator.transition_orchestration(orchestration["id"], "running")


def _run(agent_runs, thread_id: str, run_id: str, status: str = "running"):
    return agent_runs.create_agent_run(
        run_id=run_id,
        status=status,
        parent_thread_id=thread_id,
        thread_id=f"child-{run_id}",
        prompt=f"Work for {run_id}",
        display_name=run_id,
        model_override="provider:model",
    )


def test_activity_query_tracks_joined_approval_retry_and_later_parent_phase(
    tmp_path,
    reload_for_data_dir,
):
    tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, reload_for_data_dir)
    orchestration = _orchestration(orchestrator, "joined-thread", "generation-1")
    child = _run(agent_runs, "joined-thread", "joined-child")
    orchestrator.register_member(orchestration["id"], child["id"], required=True)

    activity = orchestrator.get_thread_orchestration_activity(["joined-thread"])[
        "joined-thread"
    ]
    assert activity == {
        "orchestration_id": orchestration["id"],
        "state": "active",
        "blocking": True,
        "background": False,
        "phase": "child_running",
        "active_members": 1,
        "failed_members": 0,
    }

    conn = orchestrator._conn()
    try:
        conn.execute(
            "UPDATE agent_runs SET status = 'waiting_approval' WHERE id = ?",
            (child["id"],),
        )
        conn.commit()
    finally:
        conn.close()
    _token, approval_id = tasks.create_approval_request(
        run_id=child["id"],
        task_id="",
        step_id="agent_interrupt",
        message="Child approval",
        agent_run_id=child["id"],
        resume_kind="agent_run",
        parent_thread_id="joined-thread",
    )
    assert orchestrator.get_thread_orchestration_activity(["joined-thread"])[
        "joined-thread"
    ]["phase"] == "approval_wait"

    conn = orchestrator._conn()
    try:
        conn.execute(
            "UPDATE agent_runs SET status = 'retrying' WHERE id = ?",
            (child["id"],),
        )
        conn.execute(
            "UPDATE approval_requests SET status = 'denied', responded_at = ? WHERE id = ?",
            (orchestrator._now(), approval_id),
        )
        conn.commit()
    finally:
        conn.close()
    assert orchestrator.get_thread_orchestration_activity(["joined-thread"])[
        "joined-thread"
    ]["phase"] == "retry"

    conn = orchestrator._conn()
    try:
        conn.execute(
            "UPDATE agent_runs SET status = 'completed' WHERE id = ?",
            (child["id"],),
        )
        conn.execute(
            "UPDATE agent_orchestrations SET parent_state = 'running', "
            "parent_attempt = 2, status = 'running' WHERE id = ?",
            (orchestration["id"],),
        )
        conn.commit()
    finally:
        conn.close()
    assert orchestrator.get_thread_orchestration_activity(["joined-thread"])[
        "joined-thread"
    ]["phase"] == "later_wave_parent"


def test_activity_query_requires_pending_approval_and_never_animates_interrupted_stop(
    tmp_path,
    reload_for_data_dir,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, reload_for_data_dir)
    orchestration = _orchestration(orchestrator, "repair-thread", "generation-repair")
    child = _run(agent_runs, "repair-thread", "repair-child")
    orchestrator.register_member(orchestration["id"], child["id"], required=True)
    conn = orchestrator._conn()
    try:
        conn.execute(
            "UPDATE agent_runs SET status = 'waiting_approval' WHERE id = ?",
            (child["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    no_request = orchestrator.get_thread_orchestration_activity(["repair-thread"])[
        "repair-thread"
    ]
    assert no_request["phase"] != "approval_wait"

    conn = orchestrator._conn()
    try:
        conn.execute(
            "UPDATE agent_runs SET status = 'interrupted', stop_requested = 1 WHERE id = ?",
            (child["id"],),
        )
        conn.execute(
            "UPDATE agent_orchestration_members SET status = 'interrupted' "
            "WHERE orchestration_id = ? AND run_id = ?",
            (orchestration["id"], child["id"]),
        )
        conn.execute(
            "UPDATE agent_orchestrations SET status = 'interrupted', "
            "parent_state = 'interrupted' WHERE id = ?",
            (orchestration["id"],),
        )
        conn.commit()
    finally:
        conn.close()

    interrupted = orchestrator.get_thread_orchestration_activity(["repair-thread"])[
        "repair-thread"
    ]
    assert interrupted["state"] == "attention"
    assert interrupted["blocking"] is False
    assert interrupted["phase"] == "resume_required"


def test_activity_query_keeps_detached_background_nonblocking_and_recovers(
    tmp_path,
    reload_for_data_dir,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, reload_for_data_dir)
    orchestration = _orchestration(orchestrator, "detached-thread", "generation-2")
    detached = _run(agent_runs, "detached-thread", "detached-child")
    orchestrator.register_member(
        orchestration["id"], detached["id"], required=False
    )
    orchestrator.complete_parent_pass(
        orchestration["id"],
        "The parent can finish while optional work continues.",
        foreground=True,
    )

    activity = orchestrator.get_thread_orchestration_activity()["detached-thread"]
    assert activity["state"] == "active"
    assert activity["blocking"] is False
    assert activity["background"] is True
    assert activity["phase"] == "background"
    assert activity["active_members"] == 1

    # A fresh process view reconstructs the same state from SQLite.
    _tasks, _agent_runs, restarted = _fresh_modules(tmp_path, reload_for_data_dir)
    recovered = restarted.get_thread_orchestration_activity(["detached-thread"])[
        "detached-thread"
    ]
    assert recovered == activity

    conn = restarted._conn()
    try:
        conn.execute(
            "UPDATE agent_runs SET status = 'completed' WHERE id = 'detached-child'"
        )
        conn.commit()
    finally:
        conn.close()
    terminal = restarted.get_thread_orchestration_activity(["detached-thread"])[
        "detached-thread"
    ]
    assert terminal["state"] == "terminal"
    assert terminal["background"] is False
    assert terminal["phase"] == "completed"


def test_activity_query_stop_all_becomes_terminal_only_after_durable_stop(
    tmp_path,
    reload_for_data_dir,
):
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, reload_for_data_dir)
    orchestration = _orchestration(orchestrator, "stop-thread", "generation-3")
    first = _run(agent_runs, "stop-thread", "stop-one")
    second = _run(agent_runs, "stop-thread", "stop-two")
    orchestrator.register_member(orchestration["id"], first["id"], required=True)
    orchestrator.register_member(orchestration["id"], second["id"], required=True)
    orchestrator.set_test_executors(
        parent=lambda *_args: "I stopped the remaining work and finished naturally.",
        delivery=lambda *_args: True,
    )
    orchestrator.complete_parent_pass(
        orchestration["id"],
        "The child Agents are running.",
        continuation_state={
            "config": {"configurable": {"thread_id": "stop-thread"}},
            "enabled_tool_names": ["agents"],
        },
        foreground=True,
    )

    orchestrator.stop_orchestration(orchestration["id"], run_id=first["id"])
    still_active = orchestrator.get_thread_orchestration_activity(["stop-thread"])[
        "stop-thread"
    ]
    assert still_active["state"] == "active"
    assert still_active["blocking"] is True

    orchestrator.stop_orchestration(orchestration["id"])
    orchestrator.wait_for_parent(orchestration["id"], timeout=2)
    stopped = orchestrator.get_thread_orchestration_activity(["stop-thread"])[
        "stop-thread"
    ]
    assert stopped["state"] == "terminal"
    assert stopped["phase"] in {"completed", "completed_partial"}


def test_optional_helper_that_finished_mid_turn_leaves_no_running_state(
    tmp_path,
    monkeypatch,
    reload_for_data_dir,
):
    """B333: a helper that finished before its chat's reply kept the row spinning."""
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, reload_for_data_dir)
    from row_bot.buddy import events as buddy_events

    emitted: list[str] = []
    monkeypatch.setattr(
        buddy_events,
        "emit_buddy_event",
        lambda event_type, **_kwargs: emitted.append(event_type.value),
    )
    orchestration = _orchestration(orchestrator, "review-thread", "generation-4")
    helper = _run(agent_runs, "review-thread", "review-helper")
    orchestrator.register_member(orchestration["id"], helper["id"], required=False)
    orchestrator.set_test_executors(
        parent=lambda *_args: pytest.fail("nothing is left for a second pass"),
    )
    agent_runs.finish_agent_run(helper["id"], "completed", summary="Dates look right")

    result = orchestrator.complete_parent_pass(
        orchestration["id"],
        "The fix is in, and the review agent agreed.",
        foreground=True,
    )

    assert result.waiting is False
    assert orchestrator.get_orchestration(orchestration["id"])["status"] == "completed"
    activity = orchestrator.get_thread_orchestration_activity(["review-thread"])[
        "review-thread"
    ]
    assert activity["state"] == "terminal"
    assert buddy_events.BuddyEventType.ORCHESTRATION_DONE.value in emitted


def test_required_helper_whose_result_the_reply_read_leaves_no_running_state(
    tmp_path,
    monkeypatch,
    reload_for_data_dir,
):
    """B333: the reply had read the finished helper's answer, yet a second pass was queued."""
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, reload_for_data_dir)
    import json

    from langchain_core.messages import ToolMessage

    import row_bot.agent as agent

    orchestration = _orchestration(orchestrator, "read-thread", "generation-5")
    helper = _run(agent_runs, "read-thread", "read-helper")
    orchestrator.register_member(orchestration["id"], helper["id"], required=True)
    orchestrator.set_test_executors(
        parent=lambda *_args: pytest.fail("the reply already read the helper's answer"),
    )
    finished = agent_runs.finish_agent_run(
        helper["id"], "completed", summary="Dates look right"
    )
    read_results = [
        ToolMessage(
            name="agent_wait",
            tool_call_id="wait-call",
            content=json.dumps({"ok": True, "run": {
                "id": helper["id"], "status": finished["status"],
                "summary": "Dates look right",
            }}),
        ),
    ]
    monkeypatch.setattr(
        "row_bot.threads.get_latest_checkpoint_messages",
        lambda _thread_id: read_results,
    )

    result = agent._complete_unified_parent_pass(
        "The review agent agreed: the dates are right.",
        ["agents"],
        {"configurable": {"thread_id": "read-thread", "generation_id": "generation-5"}},
    )

    assert result.waiting is False
    assert orchestrator.pending_thread_events(orchestration["id"]) == []
    assert orchestrator.get_thread_orchestration_activity(["read-thread"])[
        "read-thread"
    ]["state"] == "terminal"


def test_helper_result_the_reply_never_read_still_wakes_the_parent(
    tmp_path,
    monkeypatch,
    reload_for_data_dir,
):
    """The B333 fix keeps the parent's report on a required helper it never read."""
    _tasks, agent_runs, orchestrator = _fresh_modules(tmp_path, reload_for_data_dir)
    import row_bot.agent as agent

    orchestration = _orchestration(orchestrator, "unread-thread", "generation-6")
    helper = _run(agent_runs, "unread-thread", "unread-helper")
    orchestrator.register_member(orchestration["id"], helper["id"], required=True)
    calls: list[str] = []
    orchestrator.set_test_executors(
        parent=lambda _row, context, *_args: calls.append(context) or "Reported.",
        delivery=lambda *_args: True,
    )
    agent_runs.finish_agent_run(helper["id"], "completed", summary="Unread evidence")
    monkeypatch.setattr(
        "row_bot.threads.get_latest_checkpoint_messages", lambda _thread_id: []
    )

    result = agent._complete_unified_parent_pass(
        "I started a helper.",
        ["agents"],
        {"configurable": {"thread_id": "unread-thread", "generation_id": "generation-6"}},
    )
    final = orchestrator.wait_for_parent(orchestration["id"], minimum_attempts=1)

    assert result.waiting is True
    assert final["status"] == "completed"
    assert "Unread evidence" in calls[0]
