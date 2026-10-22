"""A computer use turn against a fake desktop, with LangGraph's way of pausing and going on.

``Turn.call`` runs the ``computer`` tool as the agent does. When the tool
asks the person (an approval card or a question), the card is recorded, the
test's answer is queued and the tool runs again from the top, as LangGraph
re-runs a tool node when its turn resumes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from langgraph.errors import GraphBubbleUp
import pytest

from row_bot.computer_use.client import CuaClient
from row_bot.computer_use.readiness import CuaReadiness, ReadinessCode, acknowledge_disclosure
from row_bot.computer_use.service import ComputerUseService
from tests.fixtures.fake_desktop import FakeBar, FakeControl, FakeDesktop, FakeWindow


class _Asked(GraphBubbleUp):
    """The turn pauses on a card, as LangGraph's ``interrupt`` pauses it."""

    def __init__(self, payload: Any) -> None:
        super().__init__("asked")
        self.payload = payload


def _go_to(title: str, controls: list[FakeControl]) -> Callable[[FakeDesktop, FakeWindow], None]:
    def go(_desktop: FakeDesktop, window: FakeWindow) -> None:
        window.title, window.controls = title, controls

    return go


def homeserve() -> FakeWindow:
    """Edge on the left monitor (negative coordinates, a high-resolution screen), on HomeServe's site."""

    claim = [
        FakeControl("RadioButton", "Leak", (300, 420, 220, 60)),
        FakeControl("RadioButton", "Blocked drain", (300, 500, 220, 60)),
        FakeControl("Edit", "Postcode", (300, 600, 400, 50)),
        FakeControl("Button", "Confirm and pay £60", (1800, 1100, 360, 80)),
    ]
    covered = [
        FakeControl("Hyperlink", "Make a claim", (1650, 880, 300, 70),
                    on_click=_go_to("HomeServe – Make a claim - Microsoft Edge", claim)),
    ]
    return FakeWindow(
        pid=2501, window_id=501, process="msedge.exe", title="HomeServe – Your cover - Microsoft Edge",
        bounds=(-2560, 40, 2400, 1300),
        controls=[
            FakeControl("TabItem", "HomeServe – Your cover", (10, 4, 300, 36)),
            FakeControl("Hyperlink", "What am I covered for?", (1700, 860, 420, 64),
                        on_click=_go_to("HomeServe – What am I covered for? - Microsoft Edge", covered)),
            FakeControl("Button", "Sign out", (2200, 120, 160, 50)),
        ],
    )


def sign_in() -> FakeWindow:
    return FakeWindow(
        pid=2601, window_id=601, process="Mail.exe", title="Mail – Sign in", bounds=(100, 100, 900, 700),
        controls=[
            FakeControl("Edit", "Email address", (100, 150, 400, 40)),
            FakeControl("Edit", "Password", (100, 220, 400, 40)),
            FakeControl("Button", "Sign in", (100, 300, 160, 44)),
            FakeControl("Edit", "Message", (100, 400, 600, 120)),
            FakeControl("Button", "Send", (600, 560, 120, 44)),
        ],
    )


def desktop_windows() -> list[FakeWindow]:
    return [
        homeserve(),
        FakeWindow(pid=3001, window_id=701, process="Notepad.exe", title="notes.txt - Notepad",
                   bounds=(200, 200, 1000, 700), controls=[FakeControl("Document", "Text editor", (0, 60, 996, 600))]),
        FakeWindow(pid=3101, window_id=801, process="ApplicationFrameHost.exe", title="Calculator",
                   bounds=(1300, 200, 400, 600), controls=[FakeControl("Button", "Seven", (20, 300, 80, 60))]),
        FakeWindow(pid=4001, window_id=901, process="Row-Bot.exe", title="Row-Bot", bounds=(0, 0, 1400, 900)),
        FakeWindow(pid=4002, window_id=902, process="python.exe", title="Row-Bot computer use",
                   bounds=(800, 1000, 420, 44)),
        FakeWindow(pid=4100, window_id=903, process="explorer.exe", title="Program Manager", bounds=(0, 0, 2560, 1440)),
        FakeWindow(pid=4200, window_id=904, process="TextInputHost.exe", title="Windows Input Experience",
                   bounds=(0, 0, 0, 0)),
        FakeWindow(pid=4300, window_id=905, process="WindowsTerminal.exe", title="Windows PowerShell",
                   bounds=(300, 300, 900, 600)),
        sign_in(),
    ]


def desktop_apps() -> dict[int, str]:
    return {2501: "Microsoft Edge", 3001: "Notepad", 3101: "ApplicationFrameHost", 4001: "Row-Bot",
            4300: "Terminal", 2601: "Mail"}


@dataclass
class Turn:
    """One chat's turn using the computer."""

    service: ComputerUseService
    desktop: FakeDesktop
    monkeypatch: pytest.MonkeyPatch
    thread_id: str = "chat-1"
    generation_id: str = "turn-1"
    mode: str = "approve"
    surface: str = "normal_chat"
    answers: list[Any] = field(default_factory=list)
    asked: list[dict[str, Any]] = field(default_factory=list)
    reviewer: Callable[[dict[str, Any]], bool] = lambda _payload: False
    reviewed: list[dict[str, Any]] = field(default_factory=list)
    # Called each time a card is up (first shown, and when the turn goes on): what happens meanwhile.
    while_asked: Callable[[dict[str, Any]], None] | None = None

    def __post_init__(self) -> None:
        from row_bot import approval_review

        self._resume: list[Any] = []
        self._counter = 0
        self._patch_turn()

        def review(payload: dict[str, Any], *, risks: Any = ()) -> Any:
            self.reviewed.append(payload)
            approved = self.reviewer(payload)
            return type("Verdict", (), {"approved": approved,
                                        "reason": "It matches the request." if approved else "Check this one."})()

        self.monkeypatch.setattr(approval_review, "review", review)

    def _patch_turn(self) -> None:
        """This turn is the one running: its chat, its approval level and its pauses."""

        import langgraph.types

        from row_bot import agent
        from row_bot.tools import approval_gate

        self.monkeypatch.setattr(agent, "get_active_runtime_context", lambda: {
            "thread_id": self.thread_id, "generation_id": self.generation_id, "runtime_surface": self.surface})
        self.monkeypatch.setattr(approval_gate, "current_approval_mode", lambda: self.mode)
        self.monkeypatch.setattr(langgraph.types, "interrupt", self.interrupt)

    def interrupt(self, payload: Any) -> Any:
        """LangGraph's ``interrupt``: the queued answer when the tool runs again, else the turn pauses."""

        if self.while_asked is not None:
            self.while_asked(payload)
        index = self._counter
        self._counter += 1
        if index < len(self._resume):
            return self._resume[index]
        raise _Asked(payload)

    def call(self, action: str, **arguments: Any) -> str:
        from row_bot.tools.computer_use_tool import computer

        self._patch_turn()
        self._resume = []
        while True:
            self._counter = 0
            try:
                return computer(action, **arguments)
            except _Asked as asked:
                self.asked.append(asked.payload)
                if not self.answers:
                    raise AssertionError(f"Nobody answered: {asked.payload.get('question') or asked.payload}")
                self._resume.append(self.answers.pop(0))

    @staticmethod
    def choose(option: int) -> dict[str, Any]:
        """The person's answer to a question card: one of its options."""

        return {"row_bot_answer": "answered", "answers": [{"choices": [option], "other": ""}]}


@pytest.fixture
def desktop() -> FakeDesktop:
    return FakeDesktop(windows=desktop_windows(), apps=desktop_apps())


@pytest.fixture
def stopped_turns() -> list[str]:
    """The chats whose turn the Stop bar (or Esc) stopped."""

    return []


@pytest.fixture
def computer(tmp_path, monkeypatch, desktop: FakeDesktop, stopped_turns: list[str]):
    """The computer use service on the fake desktop, as the ``computer`` tool finds it."""

    from row_bot.browser import model_view
    from row_bot.tools import computer_use_tool

    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    acknowledge_disclosure()
    monkeypatch.setattr("row_bot.computer_use.readiness.readiness",
                        lambda **_kw: CuaReadiness(ReadinessCode.READY, "ready", executable="cua-driver"))
    FakeBar.instances.clear()
    service = ComputerUseService(
        client_factory=lambda: CuaClient("cua-driver", transport_factory=lambda *_args: desktop),
        bar_factory=FakeBar,
        stop_turn=stopped_turns.append,
    )
    service.SETTLE_SECONDS = 0
    monkeypatch.setattr(computer_use_tool, "get_computer_use_service", lambda: service)
    monkeypatch.setattr(model_view, "model_sees_images", lambda model=None: True)
    yield service
    service.stop()
    model_view.forget("chat-1")


@pytest.fixture
def turn(computer: ComputerUseService, desktop: FakeDesktop, monkeypatch) -> Turn:
    return Turn(computer, desktop, monkeypatch)
