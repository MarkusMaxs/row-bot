"""Computer use in the conversation: its step (app, what Row-Bot does, the picture), Stop, and pauses.

The real platform, approval store, LangGraph graph and ``computer`` tool run
here, on a fake desktop behind a fake Cua Driver; the model is a scripted
one. Nothing reaches a real desktop, provider or network.
"""
# ruff: noqa: F811 -- the imported platform fixture is requested by name.
from __future__ import annotations

import base64
import json

from langchain_core.messages import AIMessage
from langgraph.prebuilt import create_react_agent
from langgraph.types import Command
import pytest

from row_bot.api.v1 import schemas as dto
from row_bot.computer_use.client import CuaClient
from row_bot.computer_use.readiness import CuaReadiness, ReadinessCode, acknowledge_disclosure
from row_bot.computer_use.service import ComputerUseService
from tests.contracts.client_platform.test_headless_lifecycle import command, platform  # noqa: F401
from tests.fixtures.fake_desktop import FakeBar, FakeDesktop
from tests.helpers.client_platform_fakes import fixture_id
from tests.subsystem.client_platform.test_finished_turns_answer_every_call import ProviderShapedModel, _call
from tests.subsystem.computer_use.conftest import desktop_apps, desktop_windows

pytestmark = pytest.mark.subsystem

CONVERSATION = "conversation-a"
OTHER = "conversation-b"


@pytest.fixture
def desktop() -> FakeDesktop:
    return FakeDesktop(windows=desktop_windows(), apps=desktop_apps())


@pytest.fixture
def computer(platform, desktop, monkeypatch):
    """The computer use service on the fake desktop, as the tool and the platform both find it."""

    from row_bot import agent
    from row_bot.browser import model_view
    from row_bot.computer_use import service as module
    from row_bot.tools import approval_gate

    acknowledge_disclosure()
    monkeypatch.setattr("row_bot.computer_use.readiness.readiness",
                        lambda **_kw: CuaReadiness(ReadinessCode.READY, "ready", executable="cua-driver"))
    FakeBar.instances.clear()
    service = ComputerUseService(
        client_factory=lambda: CuaClient("cua-driver", transport_factory=lambda *_args: desktop),
        bar_factory=FakeBar,
        stop_turn=lambda thread_id: platform.stop_conversation(thread_id),
    )
    service.SETTLE_SECONDS = 0
    monkeypatch.setattr(module, "_SERVICE", service)
    monkeypatch.setattr(model_view, "model_sees_images", lambda model=None: True)
    monkeypatch.setattr(approval_gate, "current_approval_mode", lambda: "approve")

    def context() -> dict:
        # As the agent sets it for each turn: its chat and its generation.
        from langgraph.config import get_config

        configurable = get_config().get("configurable") or {}
        return {"thread_id": configurable.get("thread_id"), "generation_id": configurable.get("generation_id"),
                "runtime_surface": "normal_chat"}

    monkeypatch.setattr(agent, "get_active_runtime_context", context)
    yield service
    service.stop()


class ComputerTurns:
    """The platform's stream and resume factories over the real graph, with the computer tool."""

    def __init__(self, calls: list[AIMessage]) -> None:
        from row_bot import threads
        from row_bot.tools.computer_use_tool import ComputerUseTool

        self.model = ProviderShapedModel(responses=[*calls, AIMessage(content="Done.", id="ai-done")])
        self.graph = create_react_agent(model=self.model, tools=ComputerUseTool().as_langchain_tools(),
                                        checkpointer=threads.checkpointer, version="v2")

    def stream(self, text, enabled_tools, config, *, stop_event=None):
        from langchain_core.messages import HumanMessage

        from row_bot.agent import _stream_graph

        submission = config["configurable"]["platform_submission_id"]
        yield from _stream_graph(self.graph, {"messages": [HumanMessage(content=text, id=submission)]},
                                 config, stop_event=stop_event)

    def resume(self, enabled_tools, config, approved, *, interrupt_ids=None, resume_values=None, stop_event=None):
        from row_bot.agent import _resume_value, _stream_graph

        yield from _stream_graph(self.graph, Command(resume=_resume_value(approved, interrupt_ids, resume_values)),
                                 config, stop_event=stop_event)


def _submit(platform, turns: ComputerTurns, label: str):
    platform.stream_factory, platform.resume_factory = turns.stream, turns.resume
    accepted = platform.execute(
        owner_id="fixture-owner", idempotency_key=fixture_id(label + ":key"), target=CONVERSATION,
        command=command("conversation.submit", label, {
            "submission_id": fixture_id(label), "text": "Use Notepad", "attachment_refs": [],
            "model_selection": {"provider_id": "fixture", "model_ref": "fixture/model"}}))
    handle = platform.registry.get(accepted["execution_id"])
    assert handle.producer_done.wait(10)
    return handle


def _resolve(platform, approval_id: str, label: str, payload: dict):
    receipt = platform.execute(owner_id="fixture-owner", idempotency_key=label, target=approval_id,
                               command=command("approval.resolve", label, payload))
    handle = platform.registry.get(receipt["execution_id"])
    assert handle.producer_done.wait(10)
    return handle


def _read(conversation: str = CONVERSATION) -> dict:
    from row_bot.application.client_computer_controls import read_computer_controls

    snapshot = read_computer_controls(None, conversation, validate=lambda: None)
    dto.ComputerUseSnapshot.model_validate_json(json.dumps(snapshot))
    return snapshot


def _preview(revision: str, conversation: str = CONVERSATION) -> dict:
    from row_bot.application.client_computer_controls import read_computer_preview

    preview = read_computer_preview(None, conversation, revision, validate=lambda: None)
    dto.ComputerUsePreview.model_validate_json(json.dumps(preview))
    return preview


def _tool_results() -> list[str]:
    from row_bot import threads

    return [message.content for message in threads.get_latest_checkpoint_messages(CONVERSATION)
            if getattr(message, "type", "") == "tool" and message.name == "computer"]


def _working(computer: ComputerUseService, conversation: str = CONVERSATION, generation: str = "turn-a"):
    computer.begin(conversation, generation)
    computer.allow(conversation, "Notepad")
    return computer.use("Notepad")


def test_a_turn_asks_to_use_an_app_and_goes_on_with_the_computer_after_the_ok(platform, computer, desktop):
    turns = ComputerTurns([_call("computer", "shot", action="screenshot", app="Notepad")])

    waiting = _submit(platform, turns, "use-notepad")

    assert waiting.status == "waiting_approval"
    view = platform.get_approval(waiting.approval_id)
    assert (view["question"], view["action_label"]) == ("Let Row-Bot use Notepad in this chat?", "Computer activity")
    card = _read()
    assert (card["active"], card["state"]) == (True, "waiting_approval")

    done = _resolve(platform, waiting.approval_id, "allow-notepad", {"decision": "approve"})

    assert done.status == "completed"
    [shot] = _tool_results()
    assert shot.startswith('Took a screenshot.\nNotepad · "notes.txt - Notepad"')
    assert desktop.front == 701
    # The finished turn gave the computer back; the bar went with it.
    assert _read()["active"] is False and FakeBar.instances[0].closed


def test_the_persons_input_asks_carry_on_and_the_turn_keeps_the_same_computer(platform, computer, desktop):
    turns = ComputerTurns([_call("computer", "shot", action="screenshot", app="Notepad"),
                           _call("computer", "click", action="click", x=20, y=100)])
    computer.allow(CONVERSATION, "Notepad")
    original = computer.screenshot

    def then_the_person_moves_the_mouse(app: str = ""):
        shot = original(app)
        FakeBar.instances[0].person_moves_the_mouse()
        return shot

    computer.screenshot = then_the_person_moves_the_mouse
    waiting = _submit(platform, turns, "carry-on")

    assert waiting.status == "waiting_approval"
    view = platform.get_approval(waiting.approval_id)
    assert view["questions"][0]["question"] == "You used the computer while Row-Bot was working in Notepad. Carry on?"
    card = _read()
    assert (card["state"], card["has_picture"]) == ("paused", False)
    assert _preview(card["revision"])["state"] == "hidden"

    done = _resolve(platform, waiting.approval_id, "carry-on-answer",
                    {"decision": "approve", "answers": [{"choices": [0], "other": ""}]})

    assert done.status == "completed"
    assert _tool_results()[-1].startswith("The person used the computer, then said to carry on. Nothing was done")
    assert desktop.inputs == []  # The click it was about to make never happened.


def test_the_step_shows_the_app_what_row_bot_does_and_a_live_picture_in_its_own_chat_only(computer):
    shot = _working(computer)
    computer.act("click", x=20, y=100)

    mine = _read()
    assert {key: mine[key] for key in ("active", "state", "app", "window", "doing", "has_picture", "problem",
                                       "can_stop")} == {
        "active": True, "state": "working", "app": "Notepad", "window": "notes.txt",
        "doing": 'Clicked "Text editor"', "has_picture": True, "problem": None, "can_stop": True}
    picture = _preview(mine["revision"])
    assert (picture["state"], picture["mime_type"]) == ("available", "image/jpeg")
    assert base64.b64decode(picture["image_base64"]).startswith(b"\xff\xd8\xff")

    other = _read(OTHER)
    assert (other["active"], other["state"], other["app"], other["has_picture"]) == (False, "stopped", "", False)
    assert _preview(other["revision"], OTHER)["state"] == "inactive"
    del shot


def test_a_bad_revision_is_refused_and_an_old_one_reads_the_step_as_it_is_now(computer):
    from row_bot.application.client_computer_controls import ClientComputerControlError

    with pytest.raises(ClientComputerControlError, match="invalid_computer_use_command"):
        _preview("not-a-revision")
    ended = _preview("0" * 64)  # The session ended since the step read it: no error, nothing to show.
    assert (ended["state"], ended["revision"], ended["image_base64"]) == ("inactive", _read()["revision"], None)


def test_stop_releases_the_computer_ends_the_turn_and_reads_its_first_outcome_again(platform, computer, tmp_path):
    from row_bot.application.client_computer_controls import execute_computer_command

    _working(computer)
    card = {"command_id": fixture_id("stop"), "client_session_id": fixture_id("client-a"), "type": "computer_use.stop"}

    stopped = execute_computer_command(platform, card, CONVERSATION, owner_id="session-a",
                                       key=card["command_id"], validate=lambda: None)

    assert (stopped["status"], stopped["computer_use"]["state"]) == ("completed", "stopped")
    assert computer.status_snapshot()["active"] is False and FakeBar.instances[0].closed
    assert execute_computer_command(platform, card, CONVERSATION, owner_id="session-a",
                                    key=card["command_id"], validate=lambda: None) == stopped
    # The picture never reaches a receipt or anything stored.
    assert "image" not in json.dumps(stopped)
    stored = b"".join(path.read_bytes() for path in tmp_path.rglob("*") if path.is_file())
    assert b"/9j/" not in stored and b"\xff\xd8\xff" not in stored


def test_the_stop_bar_stops_the_turn_from_the_desktop(platform, computer):
    turns = ComputerTurns([_call("computer", "shot", action="screenshot", app="Notepad")])
    computer.allow(CONVERSATION, "Notepad")
    original = computer.screenshot

    def then_esc(app: str = ""):
        shot = original(app)
        FakeBar.instances[0].press("esc")
        return shot

    computer.screenshot = then_esc
    handle = _submit(platform, turns, "esc")

    assert handle.cancel_scope.is_cancelled()
    assert handle.status == "stopped"
    assert computer.status_snapshot()["active"] is False


def test_stopping_a_conversation_stops_only_its_own_computer_use(platform, computer):
    _working(computer, generation="generation-a")
    platform.execute(owner_id="fixture-owner", idempotency_key=fixture_id("stop-b:key"), target=OTHER,
                     command=command("conversation.stop", "stop-b", {}))
    platform.execute(owner_id="fixture-owner", idempotency_key=fixture_id("stop-old:key"), target=CONVERSATION,
                     command=command("conversation.stop", "stop-old", {"generation_id": "generation-old"}))
    assert computer.status_snapshot()["active"] is True

    platform.execute(owner_id="fixture-owner", idempotency_key=fixture_id("stop-a:key"), target=CONVERSATION,
                     command=command("conversation.stop", "stop-a", {"generation_id": "generation-a"}))
    assert computer.status_snapshot()["active"] is False


def test_reading_the_step_needs_no_computer_use_module(monkeypatch):
    import sys

    monkeypatch.delitem(sys.modules, "row_bot.computer_use.service", raising=False)
    assert _read()["state"] == "stopped"
    assert "row_bot.computer_use.service" not in sys.modules
