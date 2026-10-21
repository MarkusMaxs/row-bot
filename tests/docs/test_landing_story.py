import hashlib
import json
import re
import subprocess
from html.parser import HTMLParser
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "docs" / "index.html").read_text(encoding="utf-8")
MEDIA = ROOT / "docs" / "media" / "landing-story"
SCENES = ["research", "create", "automate", "ship"]


class ElementCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements.append((tag, {key: value or "" for key, value in attrs}))


def _elements(tag: str | None = None, **required: str) -> list[dict[str, str]]:
    collector = ElementCollector()
    collector.feed(HTML)
    return [
        attrs
        for name, attrs in collector.elements
        if (tag is None or name == tag)
        and all(attrs.get(key.replace("_", "-")) == value for key, value in required.items())
    ]


def test_hero_has_one_primary_download_button_above_the_real_app() -> None:
    primary_buttons = [attrs for attrs in _elements("a") if "button--primary" in attrs.get("class", "").split()]
    assert len(primary_buttons) == 1
    assert primary_buttons[0]["data-placement"] == "hero"
    assert "<span>Your AI.</span> <span>Your machine.</span> <em>Your rules.</em>" in HTML
    assert "Row-Bot · Real app capture" in HTML
    assert "Recorded in-product" in HTML
    assert HTML.index('data-placement="hero"') < HTML.index("data-product-frame")


def test_scene_tabs_control_the_four_real_clip_panels() -> None:
    tabs = _elements("button", role="tab")
    assert [tab["data-scene-tab"] for tab in tabs] == SCENES
    assert [tab["aria-selected"] for tab in tabs] == ["true", "false", "false", "false"]
    panels = {panel["id"]: panel for panel in _elements("figure", role="tabpanel")}
    for tab in tabs:
        panel = panels[tab["aria-controls"]]
        assert panel["aria-labelledby"] == tab["id"]
        assert panel["data-scene-panel"] == tab["data-scene-tab"]
    # Only the first scene shows before any script runs.
    assert ["hidden" in panel for panel in panels.values()] == [False, True, True, True]
    assert "Local model" in HTML and "Frontier model" in HTML and "Approval required" in HTML


def test_no_media_plays_or_loops_without_the_controller() -> None:
    videos = _elements("video")
    assert len(videos) == 6  # four app clips and two Buddy slots
    for video in videos:
        assert "autoplay" not in video
        assert "loop" not in video
        assert "muted" in video
        assert video.get("preload") == "none"
    assert not re.search(r'<source src="', HTML), "clip sources load only when a clip plays"
    assert "<canvas" not in HTML


def test_four_real_clips_have_posters_fallbacks_and_bounded_receipt() -> None:
    receipt = json.loads((MEDIA / "recording-receipt.json").read_text(encoding="utf-8"))
    assert receipt["editing"].startswith("normal-speed editorial cuts")
    assert set(receipt["clips"]) == set(SCENES)
    for beat, record in receipt["clips"].items():
        minimum_duration = 2.5 if beat == "ship" else 8
        maximum_duration = 3 if beat == "ship" else 10
        assert minimum_duration <= record["duration_seconds"] <= maximum_duration
        assert record["dimensions"] == [1440, 810]
        assert record["fps"] == 30
        assert record["webm_bytes"] <= 1_500_000
        assert record["mp4_bytes"] <= 1_500_000
        for key in ("webm", "mp4", "poster"):
            path = ROOT / "docs" / record[key]
            assert path.is_file()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == record[f"{key}_sha256"]
        assert f'data-scene-panel="{beat}"' in HTML
        assert f"clips/{beat}.webm" in HTML
        assert f"clips/{beat}.mp4" in HTML
        assert f"screenshots/{beat}.webp" in HTML
    assert "privacy_mask" not in receipt["clips"]["ship"]
    assert receipt["clips"]["ship"]["approval_moment_seconds"] < receipt["clips"]["ship"]["duration_seconds"]


def test_reviewed_manifest_hashes_every_public_story_asset() -> None:
    manifest = json.loads((MEDIA / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["review_status"] == "approved"
    assert manifest["reviewed_at"]
    for asset in manifest["assets"]:
        still = MEDIA / asset["still"]
        assert hashlib.sha256(still.read_bytes()).hexdigest() == asset["sha256"]
        if clip := asset.get("clip"):
            video = MEDIA / clip
            assert hashlib.sha256(video.read_bytes()).hexdigest() == asset["clip_sha256"]
        if fallback := asset.get("clip_fallback"):
            video = MEDIA / fallback
            assert hashlib.sha256(video.read_bytes()).hexdigest() == asset["clip_fallback_sha256"]
    for state, expected_hash in manifest["buddy"]["motion"]["states"].items():
        video = MEDIA / "buddy" / f"{state}.webm"
        assert hashlib.sha256(video.read_bytes()).hexdigest() == expected_hash
    hevc_states = manifest["buddy"]["motion"]["hevc_alpha"]["states"]
    assert hevc_states.keys() == manifest["buddy"]["motion"]["states"].keys()
    for state, expected_hash in hevc_states.items():
        video = MEDIA / "buddy" / f"{state}.mov"
        assert hashlib.sha256(video.read_bytes()).hexdigest() == expected_hash
    idle_edit = manifest["buddy"]["motion"]["edits"]["idle"]
    assert idle_edit["source_sha256"] == "f1d4b3ade0d97361163bfd7cb5832b94197671ef922da119b8b0b698187718ca"
    assert idle_edit["result_sha256"] == manifest["buddy"]["motion"]["states"]["idle"]
    assert idle_edit["kept_segment_seconds"] == [5.333, 9.916]
    assert idle_edit["result_duration_seconds"] < idle_edit["source_duration_seconds"]
    assert len(manifest["demo_posters"]) == 4
    for poster in manifest["demo_posters"]:
        assert poster["source"].startswith("https://img.youtube.com/vi/")
        assert hashlib.sha256((MEDIA / poster["file"]).read_bytes()).hexdigest() == poster["sha256"]


def test_every_buddy_state_the_tour_uses_has_transparent_media() -> None:
    for state in ("idle", "thinking", "working", "approval", "success"):
        with Image.open(MEDIA / "buddy" / f"{state}.webp") as image:
            assert image.mode == "RGBA"
            assert image.getchannel("A").getextrema() == (0, 255)
        assert (MEDIA / "buddy" / f"{state}.webm").is_file()
        assert (MEDIA / "buddy" / f"{state}.mov").is_file()
    still = _elements("img", data_buddy_still="")
    assert len(still) == 1
    assert still[0]["src"] == "media/landing-story/buddy/thinking.webp"


def test_four_privacy_enhanced_demo_facades_remain() -> None:
    assert HTML.count("data-youtube=") == 4
    assert "youtube-nocookie.com/embed" in (ROOT / "docs" / "site.js").read_text(encoding="utf-8")
    assert all(video_id in HTML for video_id in ("vvnxFpOvzBQ", "hCHCVHZXHao", "-nzJoDAxcew", "Ex4taXHvcY8"))


def test_scrollable_linux_command_is_keyboard_focusable() -> None:
    assert 'class="linux-command" data-linux-command tabindex="0"' in HTML


def test_product_tour_runtime_contract() -> None:
    runtime_test = ROOT / "tests" / "docs" / "landing_story_runtime_test.cjs"
    subprocess.run(["node", str(runtime_test)], check=True, cwd=ROOT)
