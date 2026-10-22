"""Row-Bot's private Cua Driver client: its process, its environment and what it accepts back."""

from __future__ import annotations

import base64
import io

import pytest

from row_bot.computer_use import client as client_module
from row_bot.computer_use.client import CuaClient, build_cua_environment, parse_cua_result
from row_bot.computer_use.readiness import acknowledge_disclosure, cancel_disclosure
from row_bot.mcp_client.results import RawCallContent, RawCallResult
from tests.fixtures.fake_desktop import FakeDesktop
from tests.subsystem.computer_use.conftest import desktop_apps, desktop_windows

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]


@pytest.fixture
def desktop(tmp_path, monkeypatch) -> FakeDesktop:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path / "data"))
    acknowledge_disclosure()
    return FakeDesktop(windows=desktop_windows(), apps=desktop_apps())


def _client(desktop: FakeDesktop) -> CuaClient:
    return CuaClient("cua-driver", session_id="row-bot-test-session", transport_factory=lambda *_args: desktop)


def test_the_client_starts_a_session_of_its_own_and_never_changes_cuas_config(desktop) -> None:
    client = _client(desktop)
    client.call("list_windows", {})
    client.close()

    assert desktop.calls[0] == ("start_session", {"session": "row-bot-test-session"})
    assert desktop.calls[1] == ("list_windows", {"session": "row-bot-test-session"})
    assert desktop.calls[-1] == ("end_session", {"session": "row-bot-test-session"})
    assert all(name != "set_config" for name, _args in desktop.calls)


def test_environment_disables_update_check_without_embedded_or_parent_liveness_state() -> None:
    env = build_cua_environment("session", {
        "HOME": "/home/test", "SECRET": "no", "CUA_DRIVER_EMBEDDED": "1", "CUA_DRIVER_PARENT_LIVENESS_STDIN": "1",
        "CUA_DRIVER_HOST_BUNDLE_ID": "invented.host", "CUA_DRIVER_RS_TELEMETRY_ENABLED": "0",
    })
    assert env["CUA_DRIVER_RS_UPDATE_CHECK"] == "0"
    for name in ("CUA_DRIVER_EMBEDDED", "CUA_DRIVER_PARENT_LIVENESS_STDIN", "CUA_DRIVER_HOST_BUNDLE_ID",
                 "CUA_DRIVER_RS_TELEMETRY_ENABLED", "SECRET"):
        assert name not in env


@pytest.mark.parametrize(("system", "expected_args"), [("Windows", ["mcp"]), ("Darwin", ["mcp", "--direct"])])
def test_private_transport_uses_exact_platform_launch_profile(tmp_path, monkeypatch, system, expected_args) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    acknowledge_disclosure()
    created: list[dict] = []

    class _PrivateSession:
        def __init__(self, **kwargs) -> None:
            created.append(dict(kwargs))

        def open(self) -> None:
            return None

        def call_raw(self, name, arguments=None):
            return RawCallResult((RawCallContent(kind="text", text="ok"),), {"ok": True}, False)

        def close(self) -> None:
            return None

    monkeypatch.setattr(client_module.platform, "system", lambda: system)
    monkeypatch.setattr(client_module, "PrivateMcpSession", _PrivateSession)

    client = CuaClient("reviewed-cua")
    client.start()
    client.close()

    [made] = created
    assert made["command"].endswith("reviewed-cua") and made["args"] == expected_args
    assert made["env"]["CUA_DRIVER_RS_UPDATE_CHECK"] == "0"


def test_no_process_opens_before_the_notice_is_accepted(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ROW_BOT_DATA_DIR", str(tmp_path))
    cancel_disclosure()
    desktop = FakeDesktop(windows=desktop_windows())
    with pytest.raises(PermissionError, match="disclosure"):
        _client(desktop).call("list_windows", {})
    assert desktop.opened is False and desktop.calls == []


def test_a_window_state_gives_its_picture_and_controls_in_screen_coordinates(desktop) -> None:
    response = _client(desktop).call("get_window_state", {"pid": 3001, "window_id": 701})

    assert response.image_bytes.startswith(b"\x89PNG") and (response.image_width, response.image_height) == (998, 698)
    [editor] = response.elements
    assert (editor.role, editor.label, editor.bounds) == ("Document", "Text editor", (201.0, 261.0, 996.0, 600.0))


def test_controls_with_impossible_geometry_or_repeated_indexes_are_dropped() -> None:
    rows = [
        {"element_index": 0, "role": "Button", "label": "Kept", "frame": {"x": 1, "y": 2, "w": 3, "h": 4}},
        {"element_index": 0, "role": "Button", "label": "Repeated", "frame": {"x": 1, "y": 2, "w": 3, "h": 4}},
        {"element_index": 1, "role": "Button", "label": "Infinite", "frame": {"x": float("inf"), "y": 0, "w": 1, "h": 1}},
        {"element_index": 2, "role": "Button", "label": "Negative", "frame": {"x": 0, "y": 0, "w": -1, "h": 1}},
        {"element_index": "x", "role": "Button", "label": "No index", "frame": {"x": 0, "y": 0, "w": 1, "h": 1}},
    ]
    response = parse_cua_result(RawCallResult((), {"elements": rows}, False))
    assert [element.label for element in response.elements] == ["Kept"]


def test_an_error_keeps_its_code() -> None:
    response = parse_cua_result(RawCallResult((RawCallContent(kind="text", text="no"),),
                                              {"error": {"code": "Permission-Denied"}}, True))
    assert (response.is_error, response.error_code) == (True, "permission_denied")


def test_pictures_that_dont_match_their_type_or_are_too_big_are_refused() -> None:
    from PIL import Image

    mismatch = RawCallResult((RawCallContent(kind="image", data=base64.b64encode(b"not a png").decode(),
                                             mime_type="image/png"),), {})
    with pytest.raises(ValueError, match="don't agree"):
        parse_cua_result(mismatch)
    oversized = RawCallResult((RawCallContent(kind="image", data=base64.b64encode(
        b"\x89PNG\r\n\x1a\n" + b"0" * (8 * 1024 * 1024)).decode(), mime_type="image/png"),), {})
    with pytest.raises(ValueError, match="8 MiB"):
        parse_cua_result(oversized)
    out = io.BytesIO()
    Image.new("RGB", (5000, 10)).save(out, "PNG")
    huge = RawCallResult((RawCallContent(kind="image", data=base64.b64encode(out.getvalue()).decode(),
                                         mime_type="image/png"),), {})
    with pytest.raises(ValueError, match="out of range"):
        parse_cua_result(huge)


def test_a_lost_driver_is_reported_not_retried(desktop) -> None:
    client = _client(desktop)
    client.call("list_windows", {})

    def disconnected(_name, _arguments=None):
        raise ConnectionError("gone")

    desktop.call_raw = disconnected
    with pytest.raises(ConnectionError):
        client.call("click", {"pid": 3001, "window_id": 701, "x": 1, "y": 1})
