"""Computer use task suite on a fake desktop (N12-B): what went wrong on 22 Oct, and what replaces it.

Each test drives the ``computer`` tool as the agent does: it reads the
controls and their positions from the previous result and clicks there. The
fake Cua Driver maps those points to the screen as the real one does, so a
click that lands on the right control proves the whole chain: the model's
picture → Cua's picture → the screen (DPI and a monitor left of the main one
included).

| 22 Oct | Here |
|---|---|
| 1. "verdict: take over" after every click that worked | ``test_the_homeserve_claim…`` |
| 2. The model never saw the screen | ``test_a_model_that_sees_images_gets_the_window_after_each_action`` |
| 3. Background synthetic events | ``test_actions_are_real_input_on_the_window_brought_to_the_front`` |
| 4. 5–12 KB of policy prose and tokens | ``test_results_are_under_a_kilobyte…`` |
| 5. ``list_apps`` listed 100+ processes | ``test_list_apps_lists_windows_with_their_titles…`` |
| 9. "Please select Leak" | ``test_the_homeserve_claim…`` (no card but the first use and the payment) |
"""

from __future__ import annotations

import base64
import io
import re

import pytest

from row_bot.computer_use.readiness import CuaReadiness, ReadinessCode
from tests.fixtures.fake_desktop import FakeBar, FakeControl, FakeWindow
from tests.subsystem.computer_use.conftest import Turn

pytestmark = [pytest.mark.subsystem]

_CONTROL = re.compile(r'(?:^|; )([a-z ]+) "([^"]+)" (\d+),(\d+)')


def controls(result: str) -> dict[str, tuple[int, int]]:
    """The controls a result lists: {label: (x, y)} in the latest screenshot."""

    line = next(line for line in result.splitlines() if line.startswith("Controls: "))
    return {label: (int(x), int(y)) for _role, label, x, y in _CONTROL.findall(line[len("Controls: "):])}


def click(turn: Turn, result: str, label: str) -> str:
    x, y = controls(result)[label]
    return turn.call("click", x=x, y=y)


def questions_asked(turn: Turn) -> list[str]:
    return [card.get("question") or card["questions"][0]["question"] for card in turn.asked]


# ── The 22 Oct journey ───────────────────────────────────────────────────────


def test_the_homeserve_claim_runs_click_after_click_and_asks_only_to_use_edge_and_to_pay(turn, desktop):
    turn.answers = [True, True]

    steps = [turn.call("list_apps")]
    steps.append(turn.call("screenshot", app="Microsoft Edge"))
    steps.append(click(turn, steps[-1], "What am I covered for?"))
    steps.append(click(turn, steps[-1], "Make a claim"))
    steps.append(click(turn, steps[-1], "Leak"))
    steps.append(click(turn, steps[-1], "Postcode"))
    steps.append(turn.call("type", text="SW1A 1AA"))
    steps.append(click(turn, steps[-1], "Confirm and pay £60"))

    # Every click landed on its control, as real input on Edge in front.
    assert [entry["control"] for entry in desktop.inputs if entry["kind"] == "click"] == [
        "What am I covered for?", "Make a claim", "Leak", "Postcode", "Confirm and pay £60"]
    assert desktop.window(501).control("Postcode").value == "SW1A 1AA"
    # Only Row-Bot's first use of Edge and the payment asked; nothing handed a click back to the person.
    assert questions_asked(turn) == ["Let Row-Bot use Microsoft Edge in this chat?", "Pay in Microsoft Edge?"]
    assert turn.asked[1]["description"] == (
        'Row-Bot will click "Confirm and pay £60" in Microsoft Edge ("HomeServe – Make a claim - Microsoft Edge"). '
        "This can charge you.")
    for result in steps[2:]:
        lowered = result.casefold()
        assert "take over" not in lowered and "verdict" not in lowered and "please" not in lowered
    # Each action says what it did and shows the page it led to.
    assert steps[2].startswith("Clicked at ") and "(What am I covered for?)" in steps[2].splitlines()[0]
    assert '"HomeServe – What am I covered for? - Microsoft Edge"' in steps[2]
    assert steps[6].splitlines()[0] == "Typed 8 characters."
    assert len(steps) == 8


def test_a_click_maps_to_the_right_screen_point_on_any_monitor_and_scale(turn, desktop):
    turn.mode = "full"
    for app, window_id in (("Microsoft Edge", 501), ("Notepad", 701), ("Calculator", 801)):
        result = turn.call("screenshot", app=app)
        window = desktop.window(window_id)
        for label, (x, y) in controls(result).items():
            control = window.control(label)
            if control.on_click is not None:
                continue  # A click that changes the page is the journey's test.
            turn.call("click", x=x, y=y)
            landed = desktop.inputs[-1]
            expected = window.screen_point(control.centre())
            assert landed["control"] == label and landed["window_id"] == window_id
            assert abs(landed["screen"][0] - expected[0]) <= 2 and abs(landed["screen"][1] - expected[1]) <= 2
    # Edge sits on a monitor left of the main one, at a high resolution: its picture was scaled twice.
    assert desktop.window(501).bounds[0] < 0 and desktop.ratios[2501] > 1.5


def test_a_model_that_sees_images_gets_the_window_after_each_action(turn, monkeypatch):
    from langchain_core.messages import AIMessage, ToolMessage
    from PIL import Image

    from row_bot.browser import model_view

    monkeypatch.setattr("row_bot.vision_runtime.get_vision_service",
                        lambda: pytest.fail("no separate vision service"), raising=False)
    turn.mode = "full"
    result = turn.call("screenshot", app="Microsoft Edge")
    messages = [AIMessage(content="", tool_calls=[{"id": "call-1", "name": "computer", "args": {}}]),
                ToolMessage(content=result, tool_call_id="call-1")]

    sent = model_view.with_page_picture(messages, "chat-1", model="openai/gpt-test")

    caption, picture = sent[-1].content
    assert caption["text"] == "Microsoft Edge after that action (1280×693; x,y are in this picture)."
    data = picture["image_url"]["url"]
    assert data.startswith("data:image/jpeg;base64,")
    with Image.open(io.BytesIO(base64.b64decode(data.split(",", 1)[1]))) as image:
        assert image.size == (1280, 693)
    assert "screenshot 1280×693" in result


def test_a_model_without_vision_gets_the_controls_list_alone(turn, monkeypatch):
    from langchain_core.messages import AIMessage, ToolMessage

    from row_bot.browser import model_view

    monkeypatch.setattr(model_view, "model_sees_images", lambda model=None: False)
    turn.mode = "full"
    result = turn.call("screenshot", app="Microsoft Edge")
    messages = [AIMessage(content="", tool_calls=[{"id": "c", "name": "computer", "args": {}}]),
                ToolMessage(content=result, tool_call_id="c")]

    assert model_view.with_page_picture(messages, "chat-1", model="local/text-only") == messages
    assert "(no picture: this model reads the controls below)" in result
    assert set(controls(result)) == {"HomeServe – Your cover", "What am I covered for?", "Sign out"}


def test_actions_are_real_input_on_the_window_brought_to_the_front(turn, desktop):
    turn.mode = "full"
    result = turn.call("screenshot", app="Microsoft Edge")
    desktop.front = 701  # Another window came to the front since (a notification, a pop-up).

    click(turn, result, "Sign out")

    landed = desktop.inputs[-1]
    assert (landed["window_id"], landed["control"], landed["delivery"]) == (501, "Sign out", "foreground")
    names = [name for name, _args in desktop.calls]
    # The window first comes to the front, then the click, then the window's picture after it.
    assert names[-4:] == ["bring_to_front", "click", "list_windows", "get_window_state"]
    assert all(args.get("delivery_mode") == "foreground" for name, args in desktop.calls
               if name in {"click", "double_click", "right_click", "type_text", "press_key", "hotkey", "scroll",
                           "drag"})


def test_results_are_under_a_kilobyte_of_plain_text_without_tokens_or_policy_words(turn, desktop):
    crowded = desktop.window(701)
    crowded.controls = [FakeControl("Button", f"Button number {index}", (10 + (index % 10) * 95, 60 + (index // 10) * 30,
                                                                         90, 26)) for index in range(200)]
    turn.mode = "full"

    result = turn.call("screenshot", app="Notepad")

    assert len(result.encode("utf-8")) <= 1024
    assert re.search(r"; \d+ more$", result)
    for word in ("token", "lease", "verdict", "untrusted", "target_id", "route", "delivery"):
        assert word not in result.casefold()


def test_list_apps_lists_windows_with_their_titles_never_row_bot_or_the_shell(turn):
    listed = turn.call("list_apps")

    assert listed.splitlines() == [
        "Open windows:",
        '- Microsoft Edge: "HomeServe – Your cover - Microsoft Edge"',
        '- Notepad: "notes.txt - Notepad"',
        '- Calculator: "Calculator"',
        '- Terminal: "Windows PowerShell"',
        '- Mail: "Mail – Sign in"',
        'Use one with app="…" (the app\'s name, or words from its window\'s title).',
    ]
    assert turn.asked == []  # Listing windows asks nothing and takes no picture.


def test_an_app_is_found_by_its_name_or_words_from_its_title(turn, desktop):
    turn.mode = "full"
    assert '"notes.txt - Notepad"' in turn.call("screenshot", app="notes.txt")
    assert "Microsoft Edge" in turn.call("screenshot", app="edge")
    assert turn.call("screenshot", app="Spotify") == (
        'Error: No window of "Spotify" is open.\nFix: Call list_apps to see what\'s open, or open_app to open it.')


# ── The Stop bar and the person's own input ──────────────────────────────────


def test_the_stop_bar_shows_while_row_bot_works_and_esc_ends_the_turn_at_once(turn, desktop, computer,
                                                                                stopped_turns):
    turn.mode = "full"
    turn.call("screenshot", app="Notepad")
    [bar] = FakeBar.instances
    assert bar.shown[-1] == "Row-Bot is using Notepad"

    bar.press("esc")

    assert stopped_turns == ["chat-1"]
    assert bar.closed and desktop.closed
    assert computer.status_snapshot()["active"] is False


def test_stop_in_the_chat_releases_the_computer_and_the_bar(turn, computer):
    from row_bot.application.client_computer_controls import stop_computer_use

    turn.mode = "full"
    turn.call("screenshot", app="Notepad")

    stop_computer_use("chat-1", computer=computer)

    assert FakeBar.instances[0].closed
    assert computer.status_snapshot()["active"] is False


def test_the_persons_input_pauses_and_asks_carry_on_and_nothing_is_replayed(turn, desktop):
    turn.mode = "full"
    result = turn.call("screenshot", app="Microsoft Edge")
    [bar] = FakeBar.instances
    bar.person_moves_the_mouse()
    clicks = len(desktop.inputs)
    turn.answers = [Turn.choose(0)]

    answered = click(turn, result, "Sign out")

    [card] = turn.asked
    assert card["kind"] == "question"
    question = card["questions"][0]
    assert question["question"] == "You used the computer while Row-Bot was working in Microsoft Edge. Carry on?"
    assert [option["label"] for option in question["options"]] == ["Carry on", "Stop"]
    assert bar.shown[-2] == "Row-Bot paused · Carry on in Row-Bot, or Stop"
    assert len(desktop.inputs) == clicks  # The click it was about to make wasn't made.
    assert answered.startswith("The person used the computer, then said to carry on. Nothing was done")
    # The next action goes on as usual.
    click(turn, answered, "Sign out")
    assert desktop.inputs[-1]["control"] == "Sign out"
    assert len(turn.asked) == 1


def test_stop_on_the_carry_on_card_stops_computer_use_for_the_turn(turn, desktop, computer):
    turn.mode = "full"
    result = turn.call("screenshot", app="Notepad")
    FakeBar.instances[0].person_moves_the_mouse()
    turn.answers = [Turn.choose(1)]

    stopped = turn.call("click", x=10, y=100)

    assert stopped.startswith("The person stopped computer use.")
    assert computer.status_snapshot()["active"] is False
    assert turn.call("screenshot") == ("Cancelled: The person stopped computer use in this turn.\n"
                                       "Fix: Don't use the computer again unless they ask.")
    assert not [entry for entry in desktop.inputs if entry["kind"] == "click"]
    del result


def test_answering_row_bot_in_the_chat_isnt_the_person_using_the_computer(turn, desktop, computer):
    now = [100.0]
    computer._clock = lambda: now[0]
    turn.answers = [True]
    result = turn.call("screenshot", app="Microsoft Edge")
    result = click(turn, result, "What am I covered for?")
    bar = FakeBar.instances[0]
    # The person moves the mouse to the payment's card and clicks Allow, then lets go of it.
    turn.answers = [True]
    turn.while_asked = lambda _card: bar.person_moves_the_mouse()
    result = click(turn, click(turn, result, "Make a claim"), "Confirm and pay £60")
    now[0] += 0.4
    bar.person_moves_the_mouse()

    assert desktop.inputs[-1]["control"] == "Confirm and pay £60"
    assert questions_asked(turn) == ["Let Row-Bot use Microsoft Edge in this chat?", "Pay in Microsoft Edge?"]
    assert computer.must_ask() is False
    now[0] += 2
    bar.person_moves_the_mouse()
    assert computer.must_ask() is True


# ── Asking first (D19) and what Row-Bot never does ───────────────────────────


def test_the_first_use_of_each_app_asks_once_per_chat(turn, computer):
    turn.answers = [True]
    turn.call("screenshot", app="Notepad")
    turn.call("screenshot", app="Notepad")
    turn.call("click", x=20, y=100)
    assert questions_asked(turn) == ["Let Row-Bot use Notepad in this chat?"]
    assert turn.asked[0]["description"] == (
        "Row-Bot will use Notepad with your mouse and keyboard while you watch. A bar on the screen shows it; "
        "press Esc or Stop to stop it at any time.")

    computer.stop()  # The turn ends; a later turn in the same chat doesn't ask again.
    turn.generation_id = "turn-2"
    turn.call("screenshot", app="Notepad")
    assert len(turn.asked) == 1

    computer.stop()
    turn.thread_id, turn.answers = "chat-2", [True]
    turn.call("screenshot", app="Notepad")
    assert len(turn.asked) == 2


def test_a_denied_app_is_never_pictured_or_touched(turn, desktop):
    turn.answers = [False]

    refused = turn.call("screenshot", app="Notepad")

    assert refused == ("Approval: asked; denied by you — did not run\nThe person said no: Row-Bot won't use Notepad.")
    assert not [name for name, _args in desktop.calls if name in {"get_window_state", "bring_to_front"}]


@pytest.mark.parametrize(("mode", "expected"), [
    ("full", "Took a screenshot."),
    ("block", "BLOCKED: Using Notepad needs approval, and this chat is on Block."),
])
def test_hands_off_runs_and_block_refuses_the_first_use(turn, mode, expected):
    turn.mode = mode
    assert turn.call("screenshot", app="Notepad").startswith(expected)
    assert turn.asked == []


def test_under_auto_the_reviewer_may_allow_an_app_but_ask_still_asks_the_person(turn):
    turn.mode, turn.reviewer = "auto", lambda _payload: True
    turn.call("screenshot", app="Notepad")
    assert [payload["question"] for payload in turn.reviewed] == ["Let Row-Bot use Notepad in this chat?"]
    assert turn.asked == []

    turn.mode, turn.answers = "approve", [True]
    turn.call("screenshot", app="Notepad")
    assert questions_asked(turn) == ["Let Row-Bot use Notepad in this chat?"]


@pytest.mark.parametrize(("app", "label", "question", "doing"), [
    ("Microsoft Edge", "Confirm and pay £60", "Pay in Microsoft Edge?", 'click "Confirm and pay £60"'),
    ("Mail", "Send", "Send from Mail?", 'click "Send"'),
])
def test_paying_and_sending_ask_first_and_a_no_means_no_click(turn, desktop, app, label, question, doing):
    turn.mode = "full"
    result = turn.call("screenshot", app=app)
    if app == "Microsoft Edge":
        result = click(turn, click(turn, result, "What am I covered for?"), "Make a claim")
    clicks = len(desktop.inputs)
    turn.mode, turn.answers = "approve", [False]

    refused = click(turn, result, label)

    assert questions_asked(turn) == [question]
    assert refused == f"Approval: asked; denied by you — did not run\nThe person said no; Row-Bot didn't {doing}."
    assert len(desktop.inputs) == clicks


def test_the_payment_card_shows_the_window_with_where_row_bot_will_click(turn, computer):
    from PIL import Image, ImageChops

    turn.mode = "full"
    result = click(turn, click(turn, turn.call("screenshot", app="Microsoft Edge"), "What am I covered for?"),
                   "Make a claim")
    seen = []
    turn.while_asked = lambda _card: seen.append((computer.status_snapshot()["state"],
                                                  computer.ephemeral_picture()))
    turn.mode, turn.answers = "approve", [True]
    click(turn, result, "Confirm and pay £60")

    (state, (picture, mime, _revision)) = seen[0]  # While the card was up.
    assert (state, mime) == ("waiting_approval", "image/jpeg")
    plain = turn.service.latest().thumbnail()
    with Image.open(io.BytesIO(picture)) as marked, Image.open(io.BytesIO(plain)) as unmarked:
        left, top, right, bottom = ImageChops.difference(marked.convert("RGB"), unmarked.convert("RGB")).point(
            lambda value: 255 if value > 40 else 0).getbbox()
        x, y = controls(result)["Confirm and pay £60"]
        scale = marked.width / 1280
    # The ring sits on the button Row-Bot will click.
    assert abs((left + right) / 2 - x * scale) <= 3 and abs((top + bottom) / 2 - y * scale) <= 3


def test_under_auto_the_reviewer_sees_the_payment_and_hands_off_just_runs_it(turn, desktop):
    turn.mode = "full"
    result = click(turn, click(turn, turn.call("screenshot", app="Microsoft Edge"), "What am I covered for?"),
                   "Make a claim")
    turn.mode, turn.reviewer = "auto", lambda payload: payload["action"] == "pay"

    click(turn, result, "Confirm and pay £60")

    [reviewed] = turn.reviewed
    assert (reviewed["question"], reviewed["risk_class"]) == ("Pay in Microsoft Edge?", "high")
    assert turn.asked == [] and desktop.inputs[-1]["control"] == "Confirm and pay £60"


@pytest.mark.parametrize(("window", "keys", "question"), [
    (FakeWindow(7001, 7101, "Mail.exe", "Inbox – Mail", (0, 0, 800, 600)), "enter", "Send from Mail?"),
    (FakeWindow(7002, 7102, "explorer.exe", "Downloads - File Explorer", (0, 0, 800, 600)), "shift+delete",
     "Delete in File Explorer?"),
])
def test_keys_that_send_or_delete_files_ask_first(turn, desktop, window, keys, question):
    desktop.windows = [window]
    desktop.apps = {window.pid: "Mail" if "Mail" in window.process else "File Explorer"}
    turn.mode = "full"
    turn.call("screenshot", app=desktop.apps[window.pid])
    turn.mode, turn.answers = "approve", [True]

    turn.call("key", keys=keys)

    assert questions_asked(turn) == [question]
    assert desktop.inputs[-1]["kind"] == "key"


def test_changing_a_system_setting_asks_first(turn, desktop):
    settings = FakeWindow(7201, 7301, "ApplicationFrameHost.exe", "Settings", (0, 0, 1000, 700), controls=[
        FakeControl("Button", "Bluetooth", (600, 200, 80, 40)),
        FakeControl("ListItem", "Bluetooth & devices", (20, 200, 200, 40)),
    ])
    desktop.windows, desktop.apps = [settings], {7201: "ApplicationFrameHost"}
    turn.mode = "full"
    result = turn.call("screenshot", app="Settings")
    click(turn, result, "Bluetooth & devices")  # Finding the page just runs.
    turn.mode, turn.answers = "approve", [True]

    click(turn, result, "Bluetooth")

    assert questions_asked(turn) == ["Change a setting in Settings?"]


def test_row_bot_never_types_a_password_and_the_model_never_sees_one(turn, desktop):
    from PIL import Image

    turn.mode = "full"
    result = turn.call("screenshot", app="Mail")
    click(turn, result, "Email address")
    assert turn.call("type", text="me@example.com").startswith("Typed 14 characters.")

    click(turn, result, "Password")
    refused = turn.call("type", text="hunter2")

    assert refused == ("Blocked: That's a password field. Row-Bot never types passwords: ask the person (ask_person) "
                       "to type it on the computer, then carry on.")
    assert [text for _window, text in desktop.typed] == ["me@example.com"]
    x, y = controls(result)["Password"]
    with Image.open(io.BytesIO(turn.service.latest().image)) as image:
        covered = image.convert("RGB").getpixel((x, y))
    assert all(abs(channel - expected) < 24 for channel, expected in zip(covered, (96, 104, 116)))


def test_row_bot_doesnt_use_terminals_itself_or_keys_that_open_other_apps(turn, desktop):
    turn.mode = "full"
    assert turn.call("screenshot", app="Terminal") == (
        "Blocked: Row-Bot doesn't type into terminals: it runs commands with its own shell tool, which asks first "
        "when it should.")
    assert turn.call("screenshot", app="Row-Bot").startswith('Error: No window of "Row-Bot" is open.')
    turn.call("screenshot", app="Notepad")
    assert turn.call("key", keys="win+r") == (
        "Blocked: Row-Bot doesn't press the Windows key: it opens other apps. Use open_app to open an app.")
    assert turn.call("key", keys="ctrl+alt+delete").startswith("Blocked: Row-Bot doesn't press Ctrl+Alt+Delete")
    assert not [entry for entry in desktop.inputs if entry["kind"] == "key"]


# ── The other actions ────────────────────────────────────────────────────────


def test_keys_scroll_drag_and_other_clicks_map_as_the_model_names_them(turn, desktop):
    turn.mode = "full"
    result = turn.call("screenshot", app="Microsoft Edge")
    tab, link = controls(result)["HomeServe – Your cover"], controls(result)["Sign out"]

    assert turn.call("key", keys="ctrl+s").startswith("Pressed Ctrl+S.")
    assert turn.call("key", keys="Enter").startswith("Pressed Enter.")
    assert turn.call("scroll", scroll="up 3").startswith("Scrolled up 3.")
    assert turn.call("double_click", x=link[0], y=link[1]).startswith("Double-clicked at")
    assert turn.call("right_click", x=link[0], y=link[1]).startswith("Right-clicked at")
    assert turn.call("drag", x=tab[0], y=tab[1], to_x=link[0], to_y=link[1]).startswith("Dragged from")
    assert turn.call("wait", seconds=0.5).startswith("Waited 0.5 seconds.")

    keys = [entry["keys"] for entry in desktop.inputs if entry["kind"] == "key"]
    assert keys == [["ctrl", "s"], ["return"]]
    scroll = next(entry for entry in desktop.inputs if entry["kind"] == "scroll")
    assert (scroll["direction"], scroll["amount"]) == ("up", 3)
    drag = desktop.inputs[-1]
    window = desktop.window(501)
    for point, label in ((drag["screen"], "HomeServe – Your cover"), (drag["end"], "Sign out")):
        expected = window.screen_point(window.control(label).centre())
        assert abs(point[0] - expected[0]) <= 2 and abs(point[1] - expected[1]) <= 2


def test_open_app_opens_an_app_after_asking_and_uses_its_window(turn, desktop):
    desktop.launchable["Paint"] = lambda _desktop: FakeWindow(
        5001, 5101, "mspaint.exe", "Untitled - Paint", (100, 100, 800, 600),
        controls=[FakeControl("Button", "Brushes", (100, 100, 90, 40))])
    desktop.apps[5001] = "Paint"
    turn.answers = [True]

    opened = turn.call("open_app", app="Paint")

    assert questions_asked(turn) == ["Let Row-Bot open and use Paint in this chat?"]
    assert opened.startswith('Opened Paint.\nPaint · "Untitled - Paint"')
    assert desktop.front == 5101
    assert turn.call("open_app", app="C:/Windows/system32/cmd.exe").startswith("Error: open_app takes an app's name")


@pytest.mark.parametrize(("code", "message", "fix"), [
    (ReadinessCode.NOT_INSTALLED, "Computer use isn't set up yet: Cua Driver isn't installed.",
     "Open Settings › System › Browser and computer use › Set up… › Install."),
    (ReadinessCode.DISCLOSURE_REQUIRED, "Computer use isn't set up yet: its notice about Cua Driver needs a look.",
     "Open Settings › System › Browser and computer use › Set up…, review the notice, then install Cua Driver."),
    (ReadinessCode.PERMISSION_MISSING, "Row-Bot doesn't have permission to see or control this computer.",
     "Open Settings › System › Browser and computer use › Set up… › Check setup, and allow Row-Bot in Privacy & "
     "Security."),
])
def test_setup_problems_say_how_to_fix_them_and_the_chat_shows_it(turn, computer, monkeypatch, code, message, fix):
    from row_bot.application.client_computer_controls import read_computer_controls

    monkeypatch.setattr("row_bot.computer_use.readiness.readiness", lambda **_kw: CuaReadiness(code, "x"))

    assert turn.call("screenshot", app="Notepad") == f"Error: {message}\nFix: {fix}"
    card = read_computer_controls(None, "chat-1", validate=lambda: None, computer=computer)
    assert (card["active"], card["problem"]) == (False, {"message": message, "fix": fix})


def test_a_closed_window_a_point_off_the_picture_and_another_chat_say_what_to_do(turn, desktop, monkeypatch):
    turn.mode = "full"
    turn.call("screenshot", app="Notepad")
    assert turn.call("click", x=5000, y=10) == (
        "Error: 5000,10 is outside the screenshot (998×698).\nFix: Use x and y from the latest screenshot.")

    other = Turn(turn.service, desktop, monkeypatch, thread_id="chat-2", mode="full")
    assert other.call("list_apps") == ("Error: Row-Bot is using the computer in another chat.\n"
                                       "Fix: Wait for it to finish there, or stop it with Esc.")

    desktop.windows = [window for window in desktop.windows if window.window_id != 701]
    assert turn.call("click", x=20, y=100) == (
        'Error: The Notepad window ("notes.txt - Notepad") closed.\n'
        "Fix: Call list_apps and choose a window, or open_app.")


@pytest.mark.parametrize("surface", ["channel", "workflow", "agent"])
def test_computer_use_works_only_in_a_chat_on_this_computer(turn, desktop, surface):
    turn.surface = surface

    assert turn.call("list_apps").startswith("Error: Computer use works only in a chat in Row-Bot on this computer")
    assert desktop.opened is False


def test_a_finished_turn_gives_the_computer_back_and_a_waiting_one_keeps_it(turn, computer):
    from row_bot.application.client_computer_controls import release_after_turn

    turn.mode = "full"
    turn.call("screenshot", app="Notepad")
    release_after_turn("chat-1", "turn-1", "waiting_approval", computer=computer)
    assert computer.status_snapshot()["active"] is True

    release_after_turn("chat-1", "turn-1", "completed", computer=computer)
    assert computer.status_snapshot()["active"] is False and FakeBar.instances[0].closed


def test_the_chats_picture_stays_live_between_actions_and_hides_while_the_person_has_the_computer(
        turn, computer, desktop):
    now = [50.0]
    computer._clock = lambda: now[0]
    turn.mode = "full"
    turn.call("screenshot", app="Notepad")
    pictures = sum(1 for name, _args in desktop.calls if name == "get_window_state")

    assert computer.ephemeral_picture() is not None
    now[0] += 2
    assert computer.ephemeral_picture() is not None
    assert sum(1 for name, _args in desktop.calls if name == "get_window_state") == pictures + 1
    assert desktop.calls[-1][1]["max_elements"] == 1  # Only the picture, not the controls.

    FakeBar.instances[0].person_moves_the_mouse()
    computer.asking()
    assert computer.ephemeral_picture() is None
