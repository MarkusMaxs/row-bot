"""Vision tool — let the agent see through the user's camera or screen.

The agent calls ``analyze_image`` when it needs to look at something
the user is showing to the camera, read their screen, or answer a
visual question.  The tool captures a frame from the webcam or a
screenshot of the primary monitor, sends it to the selected Vision
model, and returns the description.
"""

from __future__ import annotations

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

from row_bot.tools.base import BaseTool
from row_bot.tools import registry
from row_bot.vision_runtime import get_vision_service


def _get_vision_service():
    return get_vision_service()


# ── Tool implementation ──────────────────────────────────────────────────────

# A capture of the person's screen or camera can show anything, so it asks every time, even in Auto, and says
# what it captures (B327). A page in Row-Bot's own browser, or a file, needs no capture.
_CAPTURES = {
    "screen": (
        "Capture your whole screen?",
        "Takes a picture of everything on your main display (every open window, not just Row-Bot's browser) "
        "and shows it to the vision model.",
    ),
    "camera": (
        "Take a picture with your camera?",
        "Takes a photo with your webcam and shows it to the vision model.",
    ),
}


def _analyze_browser_page(question: str) -> str:
    from row_bot.tools.browser_tool import _get_thread_id, get_session_manager

    image = get_session_manager().take_screenshot(_get_thread_id())
    if image is None:
        return "No page is open in Row-Bot's browser. Open it with browser_navigate first, then look at it."
    return _get_vision_service().analyze(image, f"This is the page open in Row-Bot's browser. {question}")


def _analyze_image(
    question: str, source: str = "camera", file_path: str = "",
) -> str:
    """Analyze the page in Row-Bot's browser, an image file, or (after asking) the person's screen or camera."""
    from row_bot.approval_policy import normalize_approval_mode
    from row_bot.tools.approval_gate import (
        APPROVAL_DENIED,
        APPROVAL_GIVEN,
        current_approval_mode,
        resolve_approval,
        with_approval,
    )

    source = str(source or "camera").strip().casefold()
    if source == "browser":
        return _analyze_browser_page(question)
    if source not in _CAPTURES:
        return _get_vision_service().capture_and_analyze(question, source=source, file_path=file_path)
    label, description = _CAPTURES[source]
    if normalize_approval_mode(current_approval_mode()) == "block":
        return f"BLOCKED: {label[:-1]} is unavailable while this conversation is in Block approval mode."
    outcome = resolve_approval(
        {"tool": "analyze_image", "label": label, "description": description,
         "args": {"source": source, "question": question}},
        approval_mode="approve",
    )
    if outcome != "allow":
        return with_approval(APPROVAL_DENIED, "Capture cancelled by user.")
    return with_approval(APPROVAL_GIVEN, _get_vision_service().capture_and_analyze(question, source=source))


# ── Registration ─────────────────────────────────────────────────────────────

class VisionTool(BaseTool):

    @property
    def name(self) -> str:
        return "vision"

    @property
    def display_name(self) -> str:
        return "👁️ Vision"

    @property
    def description(self) -> str:
        return (
            "Look at the page open in Row-Bot's browser, an image file, or (after "
            "asking the person) their screen or camera, to describe what's visible, "
            "read text and answer visual questions."
        )

    @property
    def enabled_by_default(self) -> bool:
        return True

    @property
    def untrusted_tool_names(self) -> set[str]:
        # Answers read text off web pages, screens and image files.
        return {"analyze_image"}

    def as_langchain_tools(self) -> list:

        class _AnalyzeInput(BaseModel):
            question: str = Field(
                description=(
                    "The question about what the camera or screen shows, "
                    "or about the contents of an image file. "
                    "Be specific — e.g. 'What text is on the paper?', "
                    "'Describe the object in front of the camera', "
                    "'What error is on the screen?', 'Describe this diagram'."
                )
            )
            source: str = Field(
                default="camera",
                description=(
                    "Where to get the image from. Use 'browser' for a page "
                    "open in Row-Bot's browser (a screenshot of a web page "
                    "you navigated to). Use 'file' for an image file in the "
                    "workspace (e.g. 'analyze photo.jpg'). Use 'screen' only "
                    "when the person asks about their own screen, monitor or "
                    "desktop, and 'camera' only when they ask you to look at "
                    "something physical; both ask the person first."
                ),
            )
            file_path: str = Field(
                default="",
                description=(
                    "Path to the image file when source='file'. Can be "
                    "workspace-relative (e.g. 'images/photo.jpg') or "
                    "absolute. Only used when source='file'."
                ),
            )

        return [
            StructuredTool.from_function(
                func=_analyze_image,
                name="analyze_image",
                description=(
                    "Look at an image and answer a visual question. "
                    "source='browser' looks at the page open in Row-Bot's "
                    "browser: use it for any screenshot of a web page. "
                    "source='file' with file_path reads a workspace image. "
                    "source='screen' captures the person's whole desktop and "
                    "source='camera' their webcam: use those only when they "
                    "ask about their screen or something in front of the "
                    "camera; each asks the person first."
                ),
                args_schema=_AnalyzeInput,
            ),
        ]

    def execute(self, query: str) -> str:
        return _analyze_image(query, source="camera", file_path="")


registry.register(VisionTool())
