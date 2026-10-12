"""The release's update parts are what the client unpacks, plans and trusts.

`scripts/build_update_parts.py` builds the code and assets archives, the lock
export and `row-bot-update-<v>.json` once per release. These tests build them
from a small staged app and read them back with the client's own code
(`row_bot.updates.manifest`, `planner`, `stager.unpack`), so a release can never
publish parts that the updater would refuse or misread.
"""
from __future__ import annotations

import json
import sys
import tarfile
from pathlib import Path

import pytest

import scripts.build_update_parts as parts
from row_bot import updater
from row_bot.updates import manifest as update_manifest
from row_bot.updates.planner import InstalledDist, plan_parts, plan_runtime
from row_bot.updates.stager import unpack

pytestmark = [pytest.mark.subsystem, pytest.mark.installer, pytest.mark.platform]

VERSION = "5.2.1"
SIX_WHEEL = "https://files.pythonhosted.org/packages/aa/six-1.17.0-py2.py3-none-any.whl"


def _pylock(*packages: str) -> str:
    return 'lock-version = "1.0"\ncreated-by = "test"\n\n' + "\n".join(packages)


def _package(name: str, version: str, *, wheel: str | None = None, sdist: bool = False, marker: str = "") -> str:
    lines = ["[[packages]]", f'name = "{name}"', f'version = "{version}"']
    if marker:
        lines.append(f'marker = "{marker}"')
    if sdist:
        lines.append(f'sdist = {{ url = "https://files.pythonhosted.org/{name}-{version}.tar.gz", '
                     f'size = 10, hashes = {{ sha256 = "{"1" * 64}" }} }}')
    if wheel:
        lines.append(f'wheels = [{{ url = "{wheel}", size = 11053, hashes = {{ sha256 = "{"2" * 64}" }} }}]')
    return "\n".join(lines) + "\n"


PYLOCK = _pylock(_package("six", "1.17.0", wheel=SIX_WHEEL), _package("wikipedia", "1.4.0", sdist=True))


def _write(path: Path, data: bytes | str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data.encode() if isinstance(data, str) else data)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    for name in parts.LINUX_SHELL_FILES:
        _write(root / "installer" / "linux" / name, f"#!/bin/sh\necho {name}\n")
    _write(root / "pylock.toml", PYLOCK)
    return root


@pytest.fixture
def app(tmp_path: Path) -> Path:
    """A staged app folder as app_payload_manifest.py lays it out."""
    root = tmp_path / "stage" / "app"
    _write(root / "launcher.py", "print('launcher')\n")
    _write(root / "src" / "row_bot" / "__init__.py", "")
    _write(root / "src" / "row_bot" / "version.py", f'__version__ = "{VERSION}"\n')
    _write(root / "src" / "row_bot" / "static" / "client-v2" / "index.html", "<html></html>\n")
    _write(root / "bundled_skills" / "notes" / "SKILL.md", "# Notes\n")
    _write(root / "tool_guides" / "shell.md", "# Shell\n")
    _write(root / "scripts" / "verify_runtime_dependencies.py", "print('ok')\n")
    for name in ("pyproject.toml", "requirements.txt"):
        _write(root / name, "# file\n")
    _write(root / "uv.lock", "version = 1\n")
    _write(root / "row-bot.ico", b"\x00\x00\x01\x00")
    _write(root / "static" / "fonts" / "inter.woff2", b"\x00font\xff")
    _write(root / "static" / "buddy.css", ".buddy {}\n")
    _write(root / "sounds" / "ding.wav", b"RIFF\x00\x01")
    return root


def _build(app: Path, project: Path, out: Path) -> dict:
    return parts.build_parts(app, out, version=VERSION, project_root=project, pylock=project / "pylock.toml")


def _platform(tmp_path: Path, key: str, python: str) -> Path:
    path = tmp_path / f"platform-{key}.json"
    path.write_text(json.dumps({"key": key, "python": python, "installer": parts.installer_name(key, VERSION),
                                "version": VERSION}), encoding="utf-8")
    return path


def _assembled(tmp_path: Path, app: Path, project: Path) -> tuple[Path, Path]:
    out = tmp_path / "parts"
    _build(app, project, out)
    platforms = [_platform(tmp_path, "windows-x86_64", "3.13.2"), _platform(tmp_path, "linux-x86_64", "3.12.13")]
    return out, parts.assemble(out, platforms, tmp_path / "release", require=["windows-x86_64"])


def test_the_client_unpacks_and_plans_from_the_parts(tmp_path, app, project) -> None:
    out, update_json = _assembled(tmp_path, app, project)

    described = update_manifest.parse_manifest(update_json.read_bytes(), expected_version=VERSION)
    assert described.code.file == f"row-bot-code-{VERSION}.tar.xz"
    assert described.platform("windows-x86_64").installer == f"Row-Bot-{VERSION}-Windows-x64.exe"
    assert described.lock_hash == "sha256:" + parts.sha256_file(app / "uv.lock")

    # The code archive unpacks into a version's app/ folder, without the assets.
    unpacked = tmp_path / "versions" / VERSION / "app"
    unpack(out / described.code.file, unpacked)
    assert (unpacked / "launcher.py").is_file() and (unpacked / "src" / "row_bot" / "version.py").is_file()
    assert (unpacked / "bundled_skills" / "notes" / "SKILL.md").is_file()
    assert (unpacked / "installer" / "linux" / "row-bot").is_file()
    assert not (unpacked / "static").exists() and not (unpacked / "sounds").exists()
    unpack(out / described.assets.file, unpacked)
    assert update_manifest.assets_content_hash(unpacked) == described.assets.content_sha256
    assert update_manifest.assets_content_hash(app) == described.assets.content_sha256

    # An install with these assets and another lock downloads the code and the lock export only.
    planned = plan_parts(
        described, current_version="5.2.0", platform_key="windows-x86_64", current_python="3.13.2",
        current_lock_hash="sha256:" + "0" * 64, current_assets_hash=described.assets.content_sha256,
    )
    assert planned.assets is None and planned.runtime is None
    assert planned.pylock == described.pylock
    runtime = plan_runtime(
        (out / described.pylock.file).read_text(encoding="utf-8"),
        environment=parts.target_environment("windows-x86_64", "3.13.2")[0],
        tags=parts.target_environment("windows-x86_64", "3.13.2")[1],
        installed={"six": InstalledDist("six", "1.16.0", "six-1.16.0.dist-info"),
                   "wikipedia": InstalledDist("wikipedia", "1.4.0", "wikipedia-1.4.0.dist-info")},
    )
    assert runtime.mode == "sync" and [pick.version_to for pick in runtime.change] == ["1.17.0"]


def test_the_archives_are_the_same_bytes_every_time(tmp_path, app, project) -> None:
    first = _build(app, project, tmp_path / "one")
    (app / "static" / "buddy.css").touch()
    second = _build(app, project, tmp_path / "two")

    assert first["code"]["sha256"] == second["code"]["sha256"]
    assert first["assets"]["sha256"] == second["assets"]["sha256"]
    with tarfile.open(tmp_path / "one" / parts.code_name(VERSION)) as tar:
        members = tar.getmembers()
    assert [member.name for member in members] == sorted(member.name for member in members)
    assert {(member.mtime, member.uid, member.gid, member.uname) for member in members} == {(0, 0, 0, "")}
    assert all(member.isfile() for member in members)


@pytest.mark.parametrize(
    ("relative", "data", "problem"),
    [
        ("src/row_bot/__pycache__/server.cpython-312.pyc", b"\x00", "bytecode"),
        ("src/row_bot/server.py", b"import os\r\nprint(os)\r\n", "CRLF"),
    ],
)
def test_bytecode_and_crlf_never_ship(tmp_path, app, project, relative, data, problem) -> None:
    _write(app / relative, data)

    with pytest.raises(parts.PartsError, match=problem):
        _build(app, project, tmp_path / "out")


def test_a_staged_app_of_another_version_is_refused(tmp_path, app, project) -> None:
    with pytest.raises(parts.PartsError, match="not 5.2.2"):
        parts.build_parts(app, tmp_path / "out", version="5.2.2", project_root=project,
                          pylock=project / "pylock.toml")


def test_assemble_refuses_a_changed_part_and_a_missing_platform(tmp_path, app, project) -> None:
    out = tmp_path / "parts"
    _build(app, project, out)
    windows = _platform(tmp_path, "windows-x86_64", "3.13.2")

    with pytest.raises(parts.PartsError, match="no build describes macos-arm64"):
        parts.assemble(out, [windows], tmp_path / "release", require=["windows-x86_64", "macos-arm64"])
    with pytest.raises(parts.PartsError, match="runs Python 3.13.2, but the wheel check planned for 3.12.13"):
        parts.assemble(out, [windows], tmp_path / "release", wheel_report={"targets": {"windows-x86_64": "3.12.13"}})
    (out / parts.code_name(VERSION)).write_bytes(b"other")
    with pytest.raises(parts.PartsError, match="does not match parts.json"):
        parts.assemble(out, [windows], tmp_path / "release")


def test_installer_names_are_the_ones_the_5_1_updater_downloads() -> None:
    assert updater._WIN_ASSET_RE.match(parts.installer_name("windows-x86_64", "5.2.0-beta.1"))
    assert updater._MAC_ARM_ASSET_RE.match(parts.installer_name("macos-arm64", "5.2.0-beta.1"))
    assert updater._LINUX_X64_ASSET_RE.match(parts.installer_name("linux-x86_64", "5.2.0-beta.1"))
    assert updater._LINUX_ARM64_ASSET_RE.match(parts.installer_name("linux-aarch64", "5.2.0"))
    # No update part looks like an installer to the 5.1 updater.
    for name in parts.release_file_names("5.2.0")[3:]:
        assert not any(pattern.match(name) for pattern in (
            updater._WIN_ASSET_RE, updater._MAC_ARM_ASSET_RE, updater._LINUX_X64_ASSET_RE))


def test_describe_platform_reads_the_runtime_it_is_given() -> None:
    entry = parts.describe_platform(Path(sys.executable), VERSION)

    assert entry["key"] == update_manifest.platform_key()
    assert entry["python"] == ".".join(str(part) for part in sys.version_info[:3])
    assert entry["installer"] == parts.installer_name(entry["key"], VERSION)


# ── D16: libraries without wheels ───────────────────────────────────────────


TARGETS = {"windows-x86_64": "3.13.2", "macos-arm64": "3.12.13", "linux-x86_64": "3.12.13"}


def test_a_changed_library_without_a_wheel_needs_the_full_installer(tmp_path) -> None:
    previous = _pylock(_package("six", "1.16.0", wheel=SIX_WHEEL), _package("wikipedia", "1.3.0", sdist=True))

    report = parts.wheel_check(PYLOCK, {"v5.2.0": previous}, TARGETS)

    assert report["needs_full_installer"] is True
    assert set(report["no_wheel"]) == {f"{key} from v5.2.0" for key in TARGETS}
    assert report["no_wheel"]["linux-x86_64 from v5.2.0"] == ["wikipedia 1.4.0"]
    new, old = tmp_path / "new.toml", tmp_path / "old.toml"
    new.write_text(PYLOCK, encoding="utf-8")
    old.write_text(previous, encoding="utf-8")
    command = ["wheel-check", "--pylock", str(new), "--previous-pylock", f"v5.2.0={old}",
               *(f"--target={key}:{python}" for key, python in TARGETS.items()), "--out", str(tmp_path / "r.json")]
    assert parts.main(command) == 1
    assert parts.main([*command, "--acknowledge"]) == 0
    assert json.loads((tmp_path / "r.json").read_text(encoding="utf-8"))["acknowledged"] is True


def test_libraries_with_wheels_or_for_other_platforms_update_in_the_background() -> None:
    windows_only = "https://files.pythonhosted.org/packages/cc/pywin-2-cp313-cp313-win_amd64.whl"
    new = _pylock(_package("six", "1.17.0", wheel=SIX_WHEEL), _package("wikipedia", "1.4.0", sdist=True),
                  _package("pywin", "2", wheel=windows_only, marker="sys_platform == 'win32'"))
    previous = _pylock(_package("six", "1.16.0", wheel=SIX_WHEEL), _package("wikipedia", "1.4.0", sdist=True),
                       _package("pywin", "1", wheel=windows_only, marker="sys_platform == 'win32'"))

    report = parts.wheel_check(new, {"v5.2.0": previous}, TARGETS)

    assert report["needs_full_installer"] is False
    assert report["platforms"]["windows-x86_64 from v5.2.0"]["change"] == ["pywin 1 -> 2", "six 1.16.0 -> 1.17.0"]
    assert report["platforms"]["linux-x86_64 from v5.2.0"]["change"] == ["six 1.16.0 -> 1.17.0"]


def test_the_target_platforms_take_the_wheels_their_runtimes_take() -> None:
    def tags(key: str, python: str) -> set[str]:
        return {str(tag) for tag in parts.target_environment(key, python)[1]}

    windows, macos, linux = tags("windows-x86_64", "3.13.2"), tags("macos-arm64", "3.12.13"), tags("linux-x86_64", "3.12.13")
    assert {"cp313-cp313-win_amd64", "cp39-abi3-win_amd64", "py3-none-any"} <= windows
    assert {"cp312-cp312-macosx_11_0_arm64", "cp312-cp312-macosx_14_0_universal2"} <= macos
    assert not any("x86_64" in tag for tag in macos)
    assert {"cp312-cp312-manylinux_2_17_x86_64", "cp312-cp312-manylinux2014_x86_64",
            "cp312-abi3-manylinux_2_28_x86_64"} <= linux
    # Ubuntu 22.04 (glibc 2.35) is the oldest supported Linux.
    assert "cp312-cp312-manylinux_2_39_x86_64" not in linux
    env = parts.target_environment("macos-arm64", "3.12.13")[0]
    assert (env["sys_platform"], env["platform_machine"], env["python_full_version"]) == ("darwin", "arm64", "3.12.13")


def test_the_lock_is_compared_with_the_newest_release_and_the_newest_stable_one() -> None:
    releases = [
        {"tagName": "v5.2.0-beta.2", "isPrerelease": True},
        {"tagName": "v5.1.0", "isPrerelease": False},
        {"tagName": "v5.2.1", "isPrerelease": False},
        {"tagName": "v5.2.0", "isPrerelease": False},
        {"tagName": "v5.3.0-beta.1", "isPrerelease": True, "isDraft": True},
        {"tagName": "nightly", "isPrerelease": True},
    ]

    assert parts.previous_tags("5.3.0-beta.1", releases) == ["v5.2.1"]
    assert parts.previous_tags("5.2.0", releases) == ["v5.2.0-beta.2", "v5.1.0"]
    assert parts.previous_tags("5.0.0", releases) == []


# ── the publish gate and the notes ──────────────────────────────────────────


def _listing(folder: Path) -> list[dict]:
    return [{"name": path.name, "size": path.stat().st_size, "sha256": parts.sha256_file(path)}
            for path in sorted(folder.iterdir()) if path.is_file() and path.name != "parts.json"]


def test_a_complete_release_passes_the_publish_gate(tmp_path, app, project) -> None:
    out = tmp_path / "parts"
    _build(app, project, out)
    platforms = [_platform(tmp_path, key, python) for key, python in TARGETS.items()]
    update_json = parts.assemble(out, platforms, tmp_path / "release", require=parts.REQUIRED_PLATFORMS)
    for key in TARGETS:
        _write(out / parts.installer_name(key, VERSION), f"installer for {key}")
    _write(out / update_json.name, update_json.read_bytes())
    listing = _listing(out)

    assert parts.verify_release(VERSION, listing, update_json.read_bytes()) == []

    without_dmg = [item for item in listing if not item["name"].endswith(".dmg")]
    assert parts.verify_release(VERSION, without_dmg, update_json.read_bytes()) == [
        f"Row-Bot-{VERSION}-macOS-arm64.dmg is missing"]
    changed = [{**item, "sha256": "f" * 64} if item["name"] == parts.code_name(VERSION) else item for item in listing]
    assert parts.verify_release(VERSION, changed, update_json.read_bytes()) == [
        f"{parts.code_name(VERSION)} differs from the update description"]


def test_the_draft_notes_are_the_version_section_and_say_when_the_full_installer_is_needed() -> None:
    notes = ("# Row-Bot - Release Notes\n\n---\n\n## v5.2.1 - Faster Updates\n\nUpdates are smaller.\n\n---\n\n"
             "## v5.2.0 - Background Updates\n\nOlder.\n")

    title, body = parts.release_notes("5.2.1", notes)
    assert title == "v5.2.1 - Faster Updates"
    assert body == "## v5.2.1 - Faster Updates\n\nUpdates are smaller.\n"
    assert not parts.notes_say_full_installer(body)

    _, body = parts.release_notes("5.2.1", notes, {"needs_full_installer": True})
    assert parts.notes_say_full_installer(body)
    assert body.startswith("## v5.2.1 - Faster Updates\n\nUpdates are smaller.\n\n**This update needs the full installer.**")


def test_container_digests_name_the_index_and_each_platform(tmp_path) -> None:
    amd, arm, index = "sha256:" + "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64
    command = ["container-digests", "--version", VERSION, "--image", "ghcr.io/siddsachar/row-bot",
               "--manifest-digest", index, "--platform", f"linux/amd64={amd}", "--out-dir", str(tmp_path)]

    assert parts.main(command) == 1  # linux/arm64 is missing
    assert parts.main([*command, "--platform", f"linux/arm64={arm}"]) == 0
    described = json.loads((tmp_path / f"row-bot-container-{VERSION}.json").read_text(encoding="utf-8"))
    assert described == {"schema": 1, "version": VERSION, "image": "ghcr.io/siddsachar/row-bot", "tag": VERSION,
                         "manifest_digest": index, "platforms": {"linux/amd64": amd, "linux/arm64": arm}}
    with pytest.raises(parts.PartsError, match="not a sha256 digest"):
        parts.container_digests(VERSION, "ghcr.io/siddsachar/row-bot", "latest", {"linux/amd64": amd, "linux/arm64": arm})
