"""Every pending approval in one list, answered in place (B255).

Approvals from a workflow, another conversation or a delegated agent used to
show only where they were raised, so work in the background waited unseen.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from tests.subsystem.client_protocol.test_protocol_application import _client, service  # noqa: F401
from tests.subsystem.client_protocol.test_protocol_security import bootstrap, client_app

pytestmark = pytest.mark.subsystem


def _pending(client, headers) -> dict[str, dict]:
    response = client.get("/api/v1/monitor/approvals", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["schema_version"] == 1 and body["total"] == len(body["items"])
    return {item["id"]: item for item in body["items"]}


def test_pending_approvals_list_every_source_without_secrets(service, monkeypatch):  # noqa: F811
    from row_bot import agent_runs, tasks, threads

    resumed: list[tuple[str, bool]] = []
    monkeypatch.setattr("row_bot.agent_runner.resume_agent_run",
                        lambda run_id, resume_token, approved: resumed.append((resume_token, approved)))
    task_id = tasks.create_task("Daily News", prompts=["Post today's news"], channels=[], enabled=False)
    # A workflow's run is a chat turn: its card waits in the workflow's own conversation (F31).
    news = tasks._prepare_task_thread(tasks.get_task(task_id))
    workflow_token, workflow = tasks.create_approval_request(
        "pass-0", "", "conversation", "Send today's news?", resume_kind="conversation",
        source_thread_id=news, parent_thread_id=news)
    trip = threads.create_thread("Trip planning", seed_default_skills=False)
    _, conversation = tasks.create_approval_request(
        "pass-1", "", "conversation", "Row-Bot wants to run a command.", resume_kind="conversation",
        source_thread_id=trip, parent_thread_id=trip, approval_payload_json={"args": "private-argument"})
    parent = threads.create_thread("Parent", seed_default_skills=False)
    agent_thread = threads.create_thread("Research", seed_default_skills=False)
    run = agent_runs.create_agent_run(parent_thread_id=parent, thread_id=agent_thread, display_name="Research",
                                      status="waiting_approval", prompt="Synthetic task")
    agent_token, agent = tasks.create_approval_request(
        run["id"], "", "agent_interrupt", "Research wants to search the web.", agent_run_id=run["id"],
        resume_kind="agent_run", source_label="Research", source_thread_id=agent_thread,
        parent_thread_id=parent)
    answered_token, _answered = tasks.create_approval_request(
        "run-2", "", "agent_interrupt", "Answered already", agent_run_id=run["id"], resume_kind="agent_run")
    assert tasks.respond_to_approval(answered_token, False)
    _, expired = tasks.create_approval_request(
        "run-3", "", "agent_interrupt", "Too late", agent_run_id=run["id"], resume_kind="agent_run",
        timeout_minutes=1)
    conn = tasks._get_conn()
    conn.execute("UPDATE approval_requests SET timeout_at=? WHERE id=?",
                 ((datetime.now() - timedelta(minutes=1)).isoformat(), expired))
    conn.commit()
    conn.close()

    with _client(service) as client:
        _, headers = bootstrap(client)
        response = client.get("/api/v1/monitor/approvals", headers=headers)
        items = _pending(client, headers)

        assert workflow_token not in response.text and "private-argument" not in response.text
        assert set(items) == {workflow, conversation, agent}
        assert {key: items[workflow][key] for key in ("source", "title", "what", "conversation_id")} == {
            "source": "conversation", "title": "⚡ Daily News", "what": "Send today's news?",
            "conversation_id": news}
        assert {key: items[conversation][key] for key in ("source", "title", "conversation_id", "task_id")} == {
            "source": "conversation", "title": "Trip planning", "conversation_id": trip, "task_id": None}
        assert {key: items[agent][key] for key in ("source", "title", "what", "conversation_id")} == {
            "source": "agent", "title": "Research", "what": "Research wants to search the web.",
            "conversation_id": agent_thread}
        # Each one says since when it waits, and none of them times out.
        assert all(datetime.fromisoformat(item["requested_at"]) for item in items.values())
        assert {item["expires_at"] for item in items.values()} == {None}

        # Approve in place through the same reviewed command the cards use.
        view = client.get(f"/api/v1/approvals/{agent}", headers=headers)
        assert view.status_code == 200, view.text
        resolved = client.post(f"/api/v1/approvals/{agent}/commands", headers={
            **headers, "Idempotency-Key": str(uuid4())}, json={
            "command_id": str(uuid4()), "client_session_id": headers["X-Client-Session"],
            "type": "approval.resolve", "expected_revision": view.json()["revision"],
            "payload": {"decision": "approve", "nonce": view.json()["nonce"]}})
        assert resolved.status_code in {200, 202}, resolved.text
        assert resumed[-1] == (agent_token, True)
        assert set(_pending(client, headers)) == {workflow, conversation}


def test_pending_approvals_need_a_signed_in_owner():
    client, _service, active = client_app(remote=True)
    with client:
        _, headers = bootstrap(client)
        active["value"] = False
        response = client.get("/api/v1/monitor/approvals", headers=headers)
    assert response.status_code == 401
    assert "items" not in response.json()
