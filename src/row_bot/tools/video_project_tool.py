"""The ``video`` tool (5.3.1 B3): a video project of scenes, shared with the Video panel.

Row-Bot writes the scenes (which piece of which video, its speed, zoom, card and line), makes
the video, reads its checks and changes it on request; the person edits the same project in
the Video panel beside the chat. Every change is a new version with a note of what changed, so
undo is one step. Making the video voices the lines in the person's chosen voice, renders it
with the music ducked under the voice, and saves it to the chat's files.

Approvals: downloading ffmpeg the first time, as in edit_video. Reading the chat's files and
the workspace, and writing new files to the chat, needs none.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

INLINE_SECONDS = 15


class _Scene(BaseModel):
    scene_id: str = Field(default="", description="Keep a scene's id when changing it (new scenes: leave empty).")
    source: str = Field(description="Its video: a recording (rec-…), an edit (edit-…), a file in this chat or a "
                                    "workspace path.")
    start: float = Field(default=0.0, description="Seconds into that video.")
    end: float | None = Field(default=None, description="Seconds into that video (empty: to its end).")
    speed: float = Field(default=1.0, description="2 = twice as fast (shows a 2× badge), 0.5 = half.")
    zoom: list[float] | None = Field(default=None, description="[x, y, width, height], 0-1: punch in on it.")
    card: str = Field(default="", description="A title laid over its first 3 seconds.")
    line: str = Field(default="", description="The words said over it (voiced when the video is made).")
    voice: str = Field(default="", description="A voice file made with speak for this line (empty: voiced for you).")


class _VideoInput(BaseModel):
    action: Literal["create", "change", "make", "status", "undo"] = "status"
    project: str = Field(default="", description="change, make, status, undo: the project (vid-…).")
    title: str = Field(default="", description="create: the video's name.")
    scenes: list[_Scene] | None = Field(default=None, description="create, change: every scene, in order.")
    music: str | None = Field(default=None, description="A music file (make_music) under the whole video; '' none.")
    music_volume: float | None = Field(default=None, description="0-1, default 0.3 (it dips under the voice).")
    captions: bool | None = Field(default=None, description="Show each line as a caption under its scene.")
    voice_lines: bool | None = Field(default=None, description="Say the lines (default true).")
    note: str = Field(default="", description="create, change: what you changed, in a few words.")
    version: int | None = Field(default=None, description="make, status: a version (default the latest).")


def _conversation() -> str:
    from row_bot.tool_outputs import current_thread_id

    return str(current_thread_id() or "")


def _wait_for(project_id: str, conversation: str, number: int, seconds: float) -> None:
    """Wait a little for the video while the turn waits; it carries on in the background after."""
    import time

    from row_bot import video_edits, video_projects

    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        project = video_projects.get(project_id, conversation)
        version = project.version(number) if project else None
        if version is None or version.status == "failed":
            return
        if version.edit_id:
            edit = video_edits.get(version.edit_id)
            if edit is not None:
                video_edits.wait(edit, max(0.0, deadline - time.monotonic()))
            return
        time.sleep(0.2)


def _make(project, number: int | None) -> str:
    from row_bot import video_projects
    from row_bot.tools.approval_gate import with_approval
    from row_bot.tools.video_editing_tool import ffmpeg_consent

    refusal, line, download = ffmpeg_consent("edit", "video")
    if refusal:
        return refusal
    version = video_projects.make(project, number, download=download)
    _wait_for(project.project_id, project.conversation_id, version.number, INLINE_SECONDS)
    fresh = video_projects.get(project.project_id, project.conversation_id) or project
    text = video_projects.summary(fresh, version.number)
    return with_approval(line, text) if line else text


def _video(action: str = "status", project: str = "", title: str = "", scenes: list | None = None,
           music: str | None = None, music_volume: float | None = None, captions: bool | None = None,
           voice_lines: bool | None = None, note: str = "", version: int | None = None) -> str:
    from row_bot import video_projects
    from row_bot.video_projects import ProjectError

    conversation = _conversation()
    if not conversation:
        return "Video projects need a conversation."
    raw = [item.model_dump() if hasattr(item, "model_dump") else dict(item) for item in scenes or []]
    try:
        if action == "create":
            found = video_projects.create(conversation, title, raw, music=music or "",
                                          music_volume=0.3 if music_volume is None else music_volume,
                                          captions=bool(captions), voice_lines=voice_lines is not False,
                                          by="row-bot", note=note)
            return _make(found, None)
        found = video_projects.get(project, conversation)
        if found is None:
            return f"{project or 'That'} isn't a video project in this chat: use action create."
        if action == "change":
            changed = video_projects.change(found, by="row-bot", note=note, scenes=raw if scenes is not None else None,
                                            title=title or None, music=music, music_volume=music_volume,
                                            captions=captions, voice_lines=voice_lines)
            return _make(found, changed.number)
        if action == "undo":
            if len(found.versions) < 2:
                return "There's no earlier version to go back to."
            back = video_projects.restore(found, found.versions[-2].number, by="row-bot")
            return video_projects.summary(found, back.number)
        if action == "make":
            return _make(found, version)
        if action == "status":
            number = version or found.latest.number
            _wait_for(found.project_id, conversation, number, 60)
            fresh = video_projects.get(found.project_id, conversation) or found
            return video_projects.summary(fresh, number)
    except ProjectError as error:
        return str(error)
    return "Use action create, change, make, status or undo."


def langchain_tool():
    from langchain_core.tools import StructuredTool

    return StructuredTool.from_function(
        func=_video,
        name="video",
        description=(
            "Make a video as scenes the person also edits in the Video panel: each scene a piece of a video "
            "(start, end), with speed, zoom, a card and its line (voiced when made). create, then change with "
            "every scene (keep scene ids) and a note; each change is a new version and makes the video again, "
            "saved to this chat. Read the checks in the result: never say it's done until they pass. undo goes "
            "back one version. For one quick edit use edit_video."
        ),
        args_schema=_VideoInput,
    )
