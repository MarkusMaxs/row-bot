"""Video projects (5.3.1 B3): a video as scenes, versioned, shared by the Video panel and Row-Bot.

Real attachment storage in an isolated data folder; the voice, ffmpeg and the edit job are fakes,
so nothing is spoken, encoded or downloaded.
"""
from __future__ import annotations

import pytest

from tests.contracts.client_platform.test_headless_lifecycle import platform  # noqa: F401

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]

MP4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 64


@pytest.fixture
def studio(platform, monkeypatch):  # noqa: F811
    """A chat holding two clips; speaking returns a tiny voice file; edits are captured, not run."""
    from row_bot import sound, video_edits
    from row_bot.application.attachments import register_attachment

    for name in ("signup.mp4", "pricing.mp4"):
        register_attachment("conversation-a", name, MP4)
    said: list[str] = []
    started: list = []

    def speak(text, **_options):
        said.append(text)
        return sound.Speech(wav=b"RIFF\x00\x00\x00\x00WAVE" + b"0" * 32, seconds=1.5, voice="test-voice")

    def start(request):
        started.append(request)
        edit = video_edits.VideoEdit(edit_id=f"edit-{len(started):08x}", conversation_id=request.conversation_id,
                                     kind="video", summary="", output_name=request.output_name, started_at=1.0,
                                     status="editing")
        video_edits._ensure_loaded()
        video_edits._RECORDS[edit.edit_id] = edit
        return edit

    monkeypatch.setattr(sound, "speak", speak)
    monkeypatch.setattr(video_edits, "start", start)
    return {"said": said, "started": started}


def _wait(project_id: str, number: int):
    import threading

    from row_bot import video_projects

    for _ in range(300):
        version = video_projects.get(project_id).version(number)
        if version.edit_id or version.status == "failed":
            return version
        threading.Event().wait(0.01)
    raise AssertionError("the video was never started")


SCENES = [
    {"source": "signup.mp4", "start": 1, "end": 4, "line": "First, sign up.", "card": "Sign up"},
    {"source": "pricing.mp4", "speed": 2, "zoom": [0.5, 0, 0.5, 0.5], "line": "Then pick a plan."},
]


def test_every_change_is_a_new_version_that_keeps_voices_whose_lines_stayed_and_going_back_is_one_more(studio):
    from row_bot import video_projects

    project = video_projects.create("conversation-a", "Launch demo", SCENES, music="", note="First cut")
    first = project.latest
    assert (first.number, first.by, first.note, len(first.scenes)) == (1, "row-bot", "First cut", 2)
    video_projects.make(project, 1)
    made = _wait(project.project_id, 1)
    assert studio["said"] == ["First, sign up.", "Then pick a plan."], "each line is voiced in the person's voice"
    assert all(scene.voice and scene.voiced == scene.line for scene in made.scenes)

    scenes = [dict(SCENES[0], scene_id=made.scenes[0].scene_id),
              dict(SCENES[1], scene_id=made.scenes[1].scene_id, line="Then pick the plan that fits.")]
    second = video_projects.change(project, by="you", base=1, scenes=scenes, note="Edited in the panel")
    assert (second.number, second.by) == (2, "you")
    assert video_projects.changed_scenes(project, second) == [made.scenes[1].scene_id]
    assert second.scenes[0].voice == made.scenes[0].voice, "an unchanged line keeps its voice"
    assert [scene.line for scene in video_projects.needs_voice(second)] == ["Then pick the plan that fits."]

    with pytest.raises(video_projects.ProjectError) as refused:
        video_projects.change(project, by="you", base=1, scenes=scenes)
    assert refused.value.stale, "a change made on an older version never overwrites a newer one"

    back = video_projects.restore(project, 1, by="you")
    assert (back.number, back.note, back.edit_id) == (3, "Back to version 1", made.edit_id), "its video is reused"
    assert [item.number for item in video_projects.get(project.project_id).versions] == [1, 2, 3]


def test_making_a_version_renders_its_scenes_in_one_edit_with_its_voices_music_and_checks(studio):
    from row_bot import video_projects
    from row_bot.application.attachments import register_attachment

    register_attachment("conversation-a", "music-calm.wav", b"RIFF\x00\x00\x00\x00WAVE" + b"0" * 32)
    project = video_projects.create("conversation-a", "Launch demo", SCENES, music="music-calm.wav",
                                    captions=True)
    video_projects.make(project, 1)
    version = _wait(project.project_id, 1)

    request = studio["started"][0]
    assert [media.label for media in request.sources] == ["signup.mp4", "pricing.mp4"]
    scenes, music = request.steps
    assert scenes.op == "scenes" and scenes.style == "caption"
    assert [(cut.input, cut.start, cut.end, cut.speed, cut.card) for cut in scenes.scenes] == [
        (0, 1.0, 4.0, 1.0, "Sign up"), (1, 0.0, None, 2.0, "")]
    assert scenes.scenes[1].zoom == (0.5, 0.0, 0.5, 0.5)
    assert (music.op, music.role, music.source, music.volume) == ("sound", "music", "music-calm.wav", 0.3)
    assert set(request.sounds) == {cut.voice for cut in scenes.scenes} | {"music-calm.wav"}
    assert request.check, "the review card's checks are read from the finished video"
    view = video_projects.view(video_projects.get(project.project_id))
    assert (view["status"], view["edit"]["edit_id"]) == ("rendering", version.edit_id)


def test_a_made_version_exports_in_another_shape_and_as_a_gif(studio):
    from row_bot import video_edits, video_projects
    from row_bot.application.attachments import register_attachment

    project = video_projects.create("conversation-a", "Launch demo", SCENES)
    with pytest.raises(video_projects.ProjectError, match="Make this version first"):
        video_projects.export(project, "9:16")
    video_projects.make(project, 1)
    made = video_edits.get(_wait(project.project_id, 1).edit_id)
    saved = register_attachment("conversation-a", "Launch demo v1.mp4", MP4)
    video_edits._set(made, status="saved", media_ref=saved["attachment_ref"], mime_type="video/mp4")

    video_projects.export(project, "9:16")
    for _ in range(300):
        if "9:16" in video_projects.get(project.project_id).latest.exports:
            break
        __import__("threading").Event().wait(0.01)
    tall = studio["started"][-1]
    assert tall.steps[0].aspect == "9:16" and tall.output_name == "Launch demo v1 9x16.mp4"
    video_projects.export(project, "gif")
    gif = studio["started"][-1]
    assert gif.gif and [media.ref for media in gif.sources] == [saved["attachment_ref"]]
    exports = video_projects.view(video_projects.get(project.project_id))["exports"]
    assert [item["shape"] for item in exports] == ["9:16", "gif"]
    assert video_projects.get(project.project_id).latest.edit_id == made.edit_id, "the version's own video stays"
    with pytest.raises(video_projects.ProjectError, match="Export as"):
        video_projects.export(project, "4:3")


def test_the_brand_kit_is_used_for_every_video_unless_a_version_turns_it_off(studio):
    import io

    from PIL import Image

    from row_bot import video_brand, video_projects
    from row_bot.application.attachments import register_attachment

    assert video_brand.load() == video_brand.Brand(), "a plain default until it's saved"
    picture = io.BytesIO()
    Image.new("RGB", (40, 20), (200, 0, 0)).save(picture, format="PNG")
    logo = register_attachment("conversation-a", "logo.png", picture.getvalue())["attachment_ref"]
    clip = next(item["ref"] for item in __import__("row_bot.media_library", fromlist=["items"]).items()
                if item["kind"] == "video")
    for wrong, words in (({"colour": "red"}, "hex colour"), ({"captions": "huge"}, "Captions are"),
                         ({"logo": clip}, "a picture from Library")):
        with pytest.raises(video_brand.BrandError, match=words):
            video_brand.save(wrong)
    saved = video_brand.save({"colour": "#112233", "captions": "bold", "logo": logo, "intro": "  Row-Bot  ",
                              "outro": "row-bot.ai", "music_mood": "upbeat"})
    assert video_brand.load() == saved and saved.intro == "Row-Bot"

    project = video_projects.create("conversation-a", "Launch demo", SCENES)
    video_projects.make(project, 1)
    _wait(project.project_id, 1)
    look = studio["started"][-1].steps[0].look
    assert (look.colour, look.captions, look.intro, look.outro) == ((17, 34, 51), "bold", "Row-Bot", "row-bot.ai")
    assert look.logo.startswith(b"\x89PNG"), "the logo, ready to lay over the video"

    plain = video_projects.change(project, by="you", brand=False, note="Without the brand")
    video_projects.make(project, plain.number)
    _wait(project.project_id, plain.number)
    assert studio["started"][-1].steps[0].look is None
    assert video_projects.view(video_projects.get(project.project_id))["brand"] is False


def test_a_recordings_beats_become_its_scenes_live_as_it_is_work_sped_up_and_cuts_left_out(studio):
    import time

    from row_bot import recordings, video_projects
    from row_bot.application.attachments import register_attachment

    saved = register_attachment("conversation-a", "generated-recording.mp4", MP4)
    record = recordings.Recording(recording_id="rec-0a0b0c0d", conversation_id="conversation-a", kind="browser",
                                  started_at=time.time() - 30, max_seconds=300, status="saved",
                                  media_ref=saved["attachment_ref"], mime_type="video/mp4",
                                  beats=[[4.0, "work", "Waiting for the build"], [16.5, "live", ""],
                                         [22.0, "cut", ""], [25.0, "live", ""]])
    recordings._ensure_loaded()
    recordings._RECORDS[record.recording_id] = record

    scenes = video_projects.scenes_from_beats("rec-0a0b0c0d", "conversation-a")
    assert [(scene["start"], scene["end"], scene["speed"]) for scene in scenes] == [
        (0.0, 4.0, 1.0), (4.0, 16.5, 4.0), (16.5, 22.0, 1.0), (25.0, None, 1.0)]
    project = video_projects.create("conversation-a", "Sign-up walkthrough", scenes)
    assert [scene.source for scene in project.latest.scenes] == ["rec-0a0b0c0d"] * 4
    with pytest.raises(video_projects.ProjectError, match="isn't a recording in this chat"):
        video_projects.scenes_from_beats("rec-0a0b0c0d", "conversation-b")


def test_the_live_preview_gets_each_scenes_video_and_its_voice_while_its_line_is_unchanged(studio):
    from row_bot import video_projects
    from row_bot.application.attachments import list_conversation_files

    project = video_projects.create("conversation-a", "Launch demo", SCENES)
    files = {item["name"]: item["attachment_ref"] for item in list_conversation_files("conversation-a")}
    before = video_projects.view(video_projects.get(project.project_id))["scenes"]
    assert [scene["source_ref"] for scene in before] == [files["signup.mp4"], files["pricing.mp4"]]
    assert [scene["voice_ref"] for scene in before] == [None, None], "nothing voiced yet"

    video_projects.make(project, 1)
    made = _wait(project.project_id, 1)
    voices = {item["name"]: item["attachment_ref"] for item in list_conversation_files("conversation-a")}
    after = video_projects.view(video_projects.get(project.project_id))["scenes"]
    assert [scene["voice_ref"] for scene in after] == [voices[scene.voice] for scene in made.scenes]

    edited = [dict(SCENES[0], scene_id=made.scenes[0].scene_id, line="A new first line."),
              dict(SCENES[1], scene_id=made.scenes[1].scene_id)]
    changed = video_projects.change(project, by="you", scenes=edited)
    scenes = video_projects.view(video_projects.get(project.project_id), changed.number)["scenes"]
    assert scenes[0]["voice_ref"] is None and scenes[1]["voice_ref"] == after[1]["voice_ref"]


def test_scenes_that_cannot_be_used_are_refused_plainly(studio):
    from row_bot import video_projects

    for scenes, words in (([], "at least one scene"), ([{"source": "missing.mp4"}], "Scene 1"),
                          ([{"source": "signup.mp4", "speed": 9}], "speed is 0.25 to 4"),
                          ([{"source": "signup.mp4", "start": 5, "end": 4}], "ends before it starts")):
        with pytest.raises(video_projects.ProjectError, match=words):
            video_projects.create("conversation-a", "Demo", scenes)


def test_the_video_tool_makes_a_project_and_its_step_shows_the_project_card(studio, monkeypatch):
    from row_bot import ffmpeg_runtime
    from row_bot.application.conversation_traces import _public_specialization, specialize_tool_result
    from row_bot.tools import video_project_tool

    monkeypatch.setattr(video_project_tool, "_conversation", lambda: "conversation-a")
    monkeypatch.setattr(video_project_tool, "INLINE_SECONDS", 0.5)
    monkeypatch.setattr(ffmpeg_runtime, "find", lambda: object())

    result = video_project_tool._video(action="create", title="Launch demo", scenes=SCENES, note="First cut")
    first = result.splitlines()[0]
    assert first.startswith("Video project vid-") and '"Launch demo", version 1 of 1, 2 scenes' in first
    assert '1. [s' in result and 'signup.mp4 1–4 s, card "Sign up" — "First, sign up."' in result
    project_id = first.split()[2].rstrip(":")
    public = _public_specialization(specialize_tool_result({"name": "video", "content": result}))
    assert (public["kind"], public["project_id"]) == ("video_project", project_id)

    assert "no earlier version" in video_project_tool._video(action="undo", project=project_id)
    assert "isn't a video project in this chat" in video_project_tool._video(action="status", project="vid-00000000")
