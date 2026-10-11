"""What a background update downloads and how it builds the runtime."""

from __future__ import annotations

import json

import pytest

from row_bot.updates.manifest import ManifestError, assets_content_hash, parse_manifest, platform_key
from row_bot.updates.planner import (
    InstalledDist,
    plan_parts,
    plan_runtime,
    prune_paths,
)
from tests.fixtures.updates import make_release, wheel_name

pytestmark = [pytest.mark.subsystem]


def _manifest(**overrides):
    release = make_release(**overrides)
    return parse_manifest(json.dumps(release.manifest), expected_version=release.version), release


def _plan(manifest, **overrides):
    options = dict(
        current_version="5.2.0",
        platform_key="linux-x86_64",
        current_python="3.12.13",
        current_lock_hash="sha256:" + "1" * 64,
        current_assets_hash="2" * 64,
    )
    options.update(overrides)
    return plan_parts(manifest, **options)


def test_code_always_assets_only_when_changed_and_the_lock_only_when_it_changed():
    manifest, _release = _manifest()
    changed = _plan(manifest)
    assert changed.code == manifest.code
    assert changed.assets == manifest.assets
    assert changed.pylock == manifest.pylock and changed.runtime is None

    same = _plan(
        manifest,
        current_assets_hash=manifest.assets.content_sha256,
        current_lock_hash=manifest.lock_hash.upper().replace("SHA256:", "sha256:"),
    )
    assert same.assets is None
    assert same.pylock is None
    assert same.runtime.mode == "reuse"


def test_a_new_python_or_an_old_source_version_needs_the_full_runtime():
    manifest, _release = _manifest(python="3.13.2")
    assert _plan(manifest).runtime.mode == "full"
    assert _plan(manifest, platform_key="macos-arm64").runtime is None  # no entry: same Python assumed

    manifest, _release = _manifest(min_source_version="5.2.0")
    assert _plan(manifest, current_version="5.1.0").runtime.mode == "full"
    assert _plan(manifest, current_version="5.2.0").runtime is None


def test_the_runtime_plan_changes_adds_and_removes_by_the_lock():
    _manifest_, release = _manifest()
    pylock = release.blobs[f"row-bot-pylock-{release.version}.toml"].decode()
    installed = {
        "pip": InstalledDist("pip", "26.0", "pip-26.0.dist-info"),
        "alpha": InstalledDist("alpha", "1.0", "alpha-1.0.dist-info"),
        "beta-lib": InstalledDist("beta-lib", "2.0", "beta_lib-2.0.dist-info"),
        "gone": InstalledDist("gone", "0.1", "gone-0.1.dist-info"),
    }
    plan = plan_runtime(pylock, environment={}, tags=["py3-none-any"], installed=installed)
    assert plan.mode == "sync"
    assert [(pick.name, pick.version_from, pick.version_to) for pick in plan.change] == [("alpha", "1.0", "1.1")]
    assert [(pick.name, pick.version_to) for pick in plan.add] == [("added", "0.5")]
    # pip is outside the lock and kept; "gone" left the lock.
    assert [dist.name for dist in plan.remove] == ["gone"]
    assert plan.change[0].url.startswith("https://files.pythonhosted.org/")
    assert plan.change[0].filename == wheel_name("alpha", "1.1")


def test_a_library_without_a_wheel_for_this_computer_means_the_full_runtime():
    pylock = "\n".join([
        "[[packages]]", 'name = "native"', 'version = "2.0"',
        'wheels = [{ url = "https://files.pythonhosted.org/native-2.0-cp313-cp313-win_amd64.whl", '
        'hashes = { sha256 = "' + "a" * 64 + '" } }]', "",
        "[[packages]]", 'name = "windows-only"', 'version = "1.0"', "marker = \"sys_platform == 'win32'\"",
        'wheels = [{ url = "https://files.pythonhosted.org/windows_only-1.0-py3-none-any.whl", '
        'hashes = { sha256 = "' + "b" * 64 + '" } }]', "",
    ])
    installed = {"native": InstalledDist("native", "1.0", "native-1.0.dist-info")}
    plan = plan_runtime(
        pylock,
        environment={"sys_platform": "linux"},
        tags=["cp312-cp312-manylinux_2_28_x86_64", "py3-none-any"],
        installed=installed,
    )
    assert plan.mode == "full"
    assert plan.no_wheel == ["native 2.0"]
    # The Windows-only package is not wanted on Linux at all.
    assert all("windows" not in item for item in plan.no_wheel)


def test_the_best_wheel_for_this_interpreter_wins():
    pylock = "\n".join([
        "[[packages]]", 'name = "fast"', 'version = "3.0"', "wheels = [",
        '{ url = "https://files.pythonhosted.org/fast-3.0-py3-none-any.whl", hashes = { sha256 = "' + "a" * 64 + '" } },',
        '{ url = "https://files.pythonhosted.org/fast-3.0-cp312-cp312-manylinux_2_28_x86_64.whl", size = 9, '
        'hashes = { sha256 = "' + "b" * 64 + '" } },',
        "]", "",
    ])
    plan = plan_runtime(
        pylock,
        environment={},
        tags=["cp312-cp312-manylinux_2_28_x86_64", "py3-none-any"],
        installed={},
    )
    assert [(pick.filename, pick.size) for pick in plan.add] == [
        ("fast-3.0-cp312-cp312-manylinux_2_28_x86_64.whl", 9)
    ]


def test_prune_leaves_out_the_changed_libraries_files_and_metadata():
    installed = {
        "alpha": InstalledDist(
            "alpha", "1.0", "alpha-1.0.dist-info",
            ("alpha/__init__.py", "alpha-1.0.dist-info/RECORD", "../../Scripts/alpha.exe", "/abs/outside"),
        ),
    }
    plan = plan_runtime(
        '[[packages]]\nname = "alpha"\nversion = "1.1"\n'
        'wheels = [{ url = "https://files.pythonhosted.org/alpha-1.1-py3-none-any.whl", hashes = { sha256 = "'
        + "c" * 64 + '" } }]\n',
        environment={}, tags=["py3-none-any"], installed=installed,
    )
    skip = prune_paths(plan, installed, site_packages="Lib/site-packages")
    assert skip == {
        "Lib/site-packages/alpha/__init__.py",
        "Lib/site-packages/alpha-1.0.dist-info/RECORD",
        "Scripts/alpha.exe",
        "Lib/site-packages/alpha-1.0.dist-info/",
    }


def test_the_update_description_is_validated():
    manifest, release = _manifest()
    assert manifest.version == release.version
    with pytest.raises(ManifestError):
        parse_manifest(json.dumps(release.manifest), expected_version="9.9.9")
    for broken in (
        {**release.manifest, "schema": 2},
        {**release.manifest, "version": "../x"},
        {**release.manifest, "code": {**release.manifest["code"], "file": "../code.tar.xz"}},
        {**release.manifest, "code": {**release.manifest["code"], "sha256": "nope"}},
        {**release.manifest, "assets": {k: v for k, v in release.manifest["assets"].items() if k != "content_sha256"}},
        {**release.manifest, "platforms": {"plan9-mips": {"python": "3.12.0"}}},
    ):
        with pytest.raises(ManifestError):
            parse_manifest(json.dumps(broken))
    with pytest.raises(ManifestError):
        parse_manifest(b"{" + b" " * 70000 + b"}")


def test_platform_keys_and_the_assets_content_hash(tmp_path):
    assert platform_key("Windows", "AMD64") == "windows-x86_64"
    assert platform_key("Darwin", "arm64") == "macos-arm64"
    assert platform_key("Linux", "aarch64") == "linux-aarch64"
    assert platform_key("Plan9", "mips") is None
    app = tmp_path / "app"
    (app / "static").mkdir(parents=True)
    (app / "static" / "a.txt").write_bytes(b"one")
    first = assets_content_hash(app)
    (app / "static" / "a.txt").write_bytes(b"one\r\n")
    assert assets_content_hash(app) != first  # CRLF copies differ (D17)
