"""Row-Bot's private connection to Cua Driver, with a hard tool allowlist.

Cua Driver (the reviewed, opt-in integration) does the input and the
accessibility reading on Windows and macOS. Row-Bot calls only the tools
listed here: windows, a window's picture and controls, bringing a window to
the front, and real mouse and keyboard input. Recording runs on a client of
its own (``call_recording``); configuration, updates, desktop-wide capture,
clipboard and process control are never reachable.
"""

from __future__ import annotations

import base64
import binascii
import io
import math
import os
import platform
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from row_bot.mcp_client.results import RawCallResult, raw_call_result
from row_bot.mcp_client.runtime import PrivateMcpSession

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_IMAGE_DIMENSION = 4096
MAX_ELEMENTS = 2_000
MAX_FIELD_CHARS = 512
MAX_GEOMETRY_ABS = 1_000_000.0
ALLOWED_IMAGE_MIME = frozenset({"image/png", "image/jpeg"})

# What a computer action may call: reading windows and controls, and real input.
ACTION_TOOLS = frozenset({
    "list_apps",
    "list_windows",
    "launch_app",
    "get_window_state",
    "bring_to_front",
    "click",
    "double_click",
    "right_click",
    "type_text",
    "press_key",
    "hotkey",
    "scroll",
    "drag",
})
INTERNAL_TOOLS = frozenset({"health_report", "check_permissions", "start_session", "end_session"})
# Screen recording (F2) runs only through ``CuaClient.call_recording`` on a client of its own.
RECORDING_TOOLS = frozenset({"start_recording", "stop_recording", "get_recording_state"})


class CuaTransport(Protocol):
    def open(self) -> None: ...
    def call_raw(self, tool_name: str, arguments: dict[str, Any] | None = None) -> Any: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class CuaElement:
    """One control in a window's accessibility tree; ``bounds`` are screen coordinates."""

    index: int
    role: str
    label: str
    value: str = field(repr=False)
    bounds: tuple[float, float, float, float]
    enabled: bool | None = None
    in_web_content: bool = False


@dataclass(frozen=True)
class CuaLaunchProfile:
    argv: tuple[str, ...]
    permission_identity: str


def cua_launch_profile() -> CuaLaunchProfile:
    """The reviewed private MCP process profile for this host."""

    if platform.system().casefold() == "darwin":
        return CuaLaunchProfile(argv=("mcp", "--direct"), permission_identity="row_bot_host")
    return CuaLaunchProfile(argv=("mcp",), permission_identity="interactive_windows_session")


@dataclass(frozen=True)
class CuaResponse:
    text: str = ""
    structured: dict[str, Any] = field(default_factory=dict)
    image_bytes: bytes | None = field(default=None, repr=False)
    image_mime: str = ""
    image_width: int = 0
    image_height: int = 0
    elements: tuple[CuaElement, ...] = ()
    is_error: bool = False
    error_code: str = ""


def build_cua_environment(
    session_id: str, environ: dict[str, str] | None = None, *, path_prefix: str = "",
) -> dict[str, str]:
    """The deliberately small child environment for Cua.

    ``path_prefix`` puts one folder first on PATH: the folder holding only the ffmpeg Row-Bot
    downloaded, which Cua's screen recording needs on Windows (F3).
    """

    source = dict(os.environ if environ is None else environ)
    common = {"PATH", "Path", "HOME", "USERPROFILE", "TMP", "TEMP", "TMPDIR", "LANG", "LC_ALL"}
    windows = {"SystemRoot", "WINDIR", "COMSPEC", "APPDATA", "LOCALAPPDATA", "SESSIONNAME"}
    desktop = {"DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
    allowed = common | windows | desktop
    result = {key: str(value) for key, value in source.items() if key in allowed and value is not None}
    result["CUA_DRIVER_RS_UPDATE_CHECK"] = "0"
    result["ROW_BOT_CUA_SESSION_ID"] = str(session_id)
    for name in ("CUA_DRIVER_EMBEDDED", "CUA_DRIVER_PARENT_LIVENESS_STDIN", "CUA_DRIVER_HOST_BUNDLE_ID",
                 "CUA_DRIVER_RS_TELEMETRY_ENABLED", "CUA_DRIVER_RS_TELEMETRY_DEBUG"):
        result.pop(name, None)
    if path_prefix:
        key = next((name for name in ("PATH", "Path") if name in result), "PATH")
        result[key] = path_prefix + (os.pathsep + result[key] if result.get(key) else "")
    return result


def _trim(value: Any) -> str:
    text = str(value or "")
    return text[:MAX_FIELD_CHARS]


def _bounds(frame: Any) -> tuple[float, float, float, float] | None:
    source = frame if isinstance(frame, dict) else {}
    try:
        values = (
            float(source.get("x") or 0),
            float(source.get("y") or 0),
            float(source.get("w") or source.get("width") or 0),
            float(source.get("h") or source.get("height") or 0),
        )
    except (TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(value) and abs(value) <= MAX_GEOMETRY_ABS for value in values):
        return None
    if values[2] < 0 or values[3] < 0:
        return None
    return values


def _decode_image(data: str, mime: str) -> tuple[bytes, int, int]:
    if mime not in ALLOWED_IMAGE_MIME:
        raise ValueError(f"Unsupported Cua image type: {mime or 'missing'}")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Cua returned a malformed image") from exc
    if len(decoded) > MAX_IMAGE_BYTES:
        raise ValueError("Cua's screenshot is larger than 8 MiB")
    magic = b"\x89PNG\r\n\x1a\n" if mime == "image/png" else b"\xff\xd8\xff"
    if not decoded.startswith(magic):
        raise ValueError("Cua's screenshot type and content don't agree")
    from PIL import Image

    with Image.open(io.BytesIO(decoded)) as image:
        width, height = image.size
        if (image.format or "").upper() != ("PNG" if mime == "image/png" else "JPEG"):
            raise ValueError("Cua's screenshot type and content don't agree")
    if width <= 0 or height <= 0 or max(width, height) > MAX_IMAGE_DIMENSION:
        raise ValueError("Cua's screenshot size is out of range")
    return decoded, int(width), int(height)


def _flag(item: dict[str, Any], name: str) -> bool | None:
    return bool(item[name]) if name in item and item[name] is not None else None


def parse_cua_result(result: Any) -> CuaResponse:
    raw = result if isinstance(result, RawCallResult) else raw_call_result(result)
    texts: list[str] = []
    image_bytes: bytes | None = None
    image_mime = ""
    width = height = 0
    for block in raw.content:
        if block.kind == "image" or block.data:
            if image_bytes is None:
                image_bytes, width, height = _decode_image(block.data, block.mime_type)
                image_mime = block.mime_type
        elif block.text:
            texts.append(_trim(block.text))
    structured = raw.structured_content if isinstance(raw.structured_content, dict) else {}
    elements: list[CuaElement] = []
    seen: set[int] = set()
    rows = structured.get("elements") if isinstance(structured.get("elements"), list) else []
    for item in rows[:MAX_ELEMENTS]:
        if not isinstance(item, dict):
            continue
        try:
            index = int(item.get("element_index"))
        except (TypeError, ValueError):
            continue
        bounds = _bounds(item.get("frame"))
        if index < 0 or index in seen or bounds is None:
            continue
        seen.add(index)
        elements.append(CuaElement(
            index=index,
            role=_trim(item.get("role")),
            label=_trim(item.get("label")),
            value=_trim(item.get("value")),
            bounds=bounds,
            enabled=_flag(item, "enabled"),
            in_web_content=bool(item.get("in_web_content")),
        ))
    error = structured.get("error") if isinstance(structured.get("error"), dict) else {}
    error_code = str(error.get("code") or structured.get("error_code")
                     or (structured.get("code") if raw.is_error else "") or "")
    return CuaResponse(
        text="\n".join(texts),
        structured=dict(structured),
        image_bytes=image_bytes,
        image_mime=image_mime,
        image_width=width,
        image_height=height,
        elements=tuple(elements),
        is_error=bool(raw.is_error),
        error_code=error_code.strip().casefold().replace("-", "_"),
    )


class CuaClient:
    """One private Cua MCP connection with a hard tool allowlist."""

    def __init__(
        self,
        executable: str | Path,
        *,
        session_id: str | None = None,
        transport_factory: Callable[[str, str, dict[str, str]], CuaTransport] | None = None,
        contract_version: str = "0.20.0",
    ) -> None:
        self.executable = str(Path(executable))
        self.session_id = session_id or f"row-bot-{uuid.uuid4().hex}"
        self._transport_factory = transport_factory or self._default_transport
        self.launch_profile = cua_launch_profile()
        self.contract_version = str(contract_version)
        self._transport: CuaTransport | None = None
        # A folder to put first on Cua's PATH (the screen recording's ffmpeg); empty for every other client.
        self.path_prefix = ""

    def _default_transport(self, executable: str, _session_id: str, env: dict[str, str]) -> CuaTransport:
        return PrivateMcpSession(command=executable, args=list(self.launch_profile.argv), env=env, timeout=120.0)

    @property
    def started(self) -> bool:
        return self._transport is not None

    def start(self) -> None:
        if self._transport is not None:
            return
        from row_bot.computer_use.readiness import require_cua_disclosure

        require_cua_disclosure()
        transport = self._transport_factory(
            self.executable, self.session_id, build_cua_environment(self.session_id, path_prefix=self.path_prefix),
        )
        transport.open()
        self._transport = transport
        try:
            self.call_internal("start_session", {"session": self.session_id})
        except BaseException:
            self.close()
            raise

    def close(self, *, graceful: bool = True) -> None:
        transport = self._transport
        self._transport = None
        if transport is None:
            return
        if graceful:
            try:
                transport.call_raw("end_session", {"session": self.session_id})
            except Exception:
                pass
        transport.close()

    def _call(self, tool_name: str, arguments: dict[str, Any]) -> CuaResponse:
        if self._transport is None:
            self.start()
        assert self._transport is not None
        return parse_cua_result(self._transport.call_raw(tool_name, arguments))

    def call(self, tool_name: str, arguments: dict[str, Any] | None = None) -> CuaResponse:
        """One reviewed window, picture or input tool."""

        if tool_name not in ACTION_TOOLS:
            raise PermissionError(f"Cua tool is not allowlisted: {tool_name}")
        safe = dict(arguments or {})
        safe.setdefault("session", self.session_id)
        return self._call(tool_name, safe)

    def call_internal(self, tool_name: str, arguments: dict[str, Any] | None = None) -> CuaResponse:
        if tool_name not in INTERNAL_TOOLS:
            raise PermissionError(f"Cua internal tool is not allowlisted: {tool_name}")
        return self._call(tool_name, dict(arguments or {}))

    def call_recording(self, tool_name: str, arguments: dict[str, Any] | None = None) -> CuaResponse:
        """Service-only screen recording (F2), on a client of its own.

        Starting always records video into one folder the recording service
        chose; stopping and reading the state take no arguments.
        """

        if tool_name not in RECORDING_TOOLS:
            raise PermissionError(f"Cua recording tool is not approved: {tool_name}")
        if tool_name == "start_recording":
            args = dict(arguments or {})
            output_dir = args.get("output_dir")
            if set(args) != {"output_dir"} or not isinstance(output_dir, str) or not Path(output_dir).is_absolute():
                raise PermissionError("Cua recording needs exactly one absolute output folder")
            safe: dict[str, Any] = {"output_dir": output_dir, "record_video": True}
        else:
            safe = {}
        return self._call(tool_name, safe)


def default_client() -> CuaClient:
    """A client for the installed, reviewed Cua Driver; raises ``RuntimeError`` with the reason."""

    from row_bot.computer_use.readiness import ReadinessCode, load_cua_manifest, readiness

    state = readiness(enabled=True)
    if state.code not in {ReadinessCode.READY, ReadinessCode.DEGRADED}:
        raise RuntimeError(state.message)
    return CuaClient(state.executable, contract_version=str(load_cua_manifest()["version"]))
