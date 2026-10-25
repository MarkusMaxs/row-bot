"""The computer tool's contract: one small schema every provider binds, and a closed driver allowlist."""

from __future__ import annotations

import pytest

from row_bot.computer_use.client import ACTION_TOOLS, INTERNAL_TOOLS, RECORDING_TOOLS, CuaClient
from row_bot.providers.models import TransportMode
from row_bot.providers.tool_schema import apply_tool_schema_compatibility
from row_bot.tools.computer_use_tool import ComputerInput, ComputerUseTool

pytestmark = pytest.mark.contracts


def test_the_model_gets_one_small_computer_tool_and_one_to_record_the_screen() -> None:
    tools = ComputerUseTool().as_langchain_tools()
    assert [tool.name for tool in tools] == ["computer", "computer_record"]
    schema = ComputerInput.model_json_schema()
    assert schema["required"] == ["action"]
    assert list(schema["properties"]) == ["action", "app", "x", "y", "to_x", "to_y", "text", "keys", "scroll",
                                          "seconds"]
    for name in ("screenshot", "click", "double_click", "right_click", "type", "key", "scroll", "drag", "wait",
                 "open_app", "list_apps"):
        assert name in schema["properties"]["action"]["description"]
    # Nothing to fill with -1 or "" on every call: every field but the action is optional and empty by default.
    assert all(spec.get("default") is None for name, spec in schema["properties"].items() if name != "action")
    assert len(ComputerUseTool().description.split()) <= 90


def test_computer_use_is_off_by_default_and_its_results_are_fenced() -> None:
    tool = ComputerUseTool()
    assert tool.name == "computer_use"  # The saved switch keeps its name from before 5.3.
    assert tool.enabled_by_default is False
    assert tool.untrusted_tool_names == {"computer"}


def test_the_schema_binds_on_every_provider_transport() -> None:
    tools = ComputerUseTool().as_langchain_tools()
    for transport in TransportMode:
        result = apply_tool_schema_compatibility(tools, transport,
                                                 explicitly_requested_names=["computer", "computer_record"])
        assert [item.name for item in result.tools] == ["computer", "computer_record"]
        assert result.rejected_tool_names == ()


def test_the_driver_allowlist_is_closed() -> None:
    assert ACTION_TOOLS == {"list_apps", "list_windows", "launch_app", "get_window_state", "bring_to_front", "click",
                            "double_click", "right_click", "type_text", "press_key", "hotkey", "scroll", "drag"}
    assert INTERNAL_TOOLS == {"health_report", "check_permissions", "start_session", "end_session"}
    client = CuaClient("cua-driver", transport_factory=lambda *_args: pytest.fail("no process"))
    for refused in ("set_config", "get_desktop_state", "page", "kill_app", "move_cursor", "zoom", "check_for_update",
                    "install_ffmpeg", "set_value", "invoke_menu", *RECORDING_TOOLS):
        with pytest.raises(PermissionError):
            client.call(refused, {})
