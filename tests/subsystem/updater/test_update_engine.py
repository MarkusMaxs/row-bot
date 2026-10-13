"""The background update engine end to end against fakes (no network, no pip).

idle → downloading → staging → ready → applying → verified, plus failed with a
reason and retry, persisted across restarts. Staging builds the new version
next to the running one without writing to it, and a failure leaves
``state.json`` untouched.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from row_bot import install_layout
from row_bot.install_layout import InstallLayout
from row_bot.updates import switcher
from row_bot.updates.downloader import DownloadError
from row_bot.updates.engine import EngineError, UpdateEngine
from row_bot.updates.stager import Stager
from tests.fixtures.updates import (
    FakeRunner,
    FakeTransport,
    make_install,
    make_release,
    serve,
    site_packages,
    tree_fingerprint,
)

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]


def _engine(layout, transport, runner=None, *, activity=None, relaunches=None, **overrides):
    runner = runner or FakeRunner()

    def stager(layout_, **kwargs):
        return Stager(layout_, runner=runner, clone_method="hardlink", **kwargs)

    options = dict(
        transport=transport,
        stager_factory=stager,
        activity=activity or (lambda: []),
        relaunch=lambda: (relaunches.append(True) if relaunches is not None else None) or True,
        environment=lambda: ({}, ["py3-none-any"]),
        python_version=lambda: "3.12.13",
        platform_id=lambda: "linux-x86_64",
        background=False,
        sleep=lambda _seconds: None,
    )
    options.update(overrides)
    return UpdateEngine(layout, **options)


def _state_bytes(layout) -> bytes:
    return (layout.root / install_layout.STATE_FILE).read_bytes()


def test_a_release_is_downloaded_staged_and_ready_without_touching_the_running_version(tmp_path):
    layout = make_install(tmp_path)
    before = tree_fingerprint(layout.version_root)
    release = make_release()
    transport = FakeTransport(serve(release))
    runner = FakeRunner()
    engine = _engine(layout, transport, runner)

    assert engine.start(release.files)

    status = engine.status()
    assert status.phase == "ready", status
    assert status.version == "5.2.1"
    staged = layout.versions_dir / "5.2.1"
    assert (staged / "app" / "launcher.py").read_bytes() == b"# launcher 2\n"
    assert (staged / "app" / "static" / "buddy.json").read_bytes() == b'{"buddy": 2}\n'
    site = site_packages(staged)
    names = sorted(path.name for path in site.glob("*.dist-info"))
    assert names == ["added-0.5.dist-info", "alpha-1.1.dist-info", "beta_lib-2.0.dist-info", "pip-26.0.dist-info"]
    metadata = json.loads((staged / "version.json").read_text(encoding="utf-8"))
    assert metadata["source"] == "update" and metadata["runtime"] == "sync"
    assert not (layout.versions_dir / "5.2.1.staging").exists()
    state = install_layout.read_state(layout.root)
    assert state.active == "5.2.0" and state.ready["version"] == "5.2.1"
    assert state.pending_verification is False and state.previous is None
    # Only the changed wheels were fetched, and nothing of the running version changed.
    assert transport.count("files.pythonhosted.org") == 2
    assert tree_fingerprint(layout.version_root) == before
    # pip ran with hashes from local files only, then compile and both self-tests.
    assert runner.labels() == ["pip", "compile", "compile", "dependencies", "server_import"]
    pip = runner.commands[0]
    assert {"--no-index", "--no-deps", "--require-hashes"} <= set(pip)
    # Downloads are gone once the version is ready.
    assert not (layout.root / ".updates" / "5.2.1").exists()


def test_unchanged_assets_and_lock_reuse_the_running_versions_files(tmp_path):
    layout = make_install(tmp_path)
    app = layout.version_root / "app"
    release = make_release(
        lock=b"lock-1",
        assets={"static/buddy.json": (app / "static" / "buddy.json").read_bytes(),
                "sounds/ding.wav": (app / "sounds" / "ding.wav").read_bytes()},
    )
    transport = FakeTransport(serve(release))
    runner = FakeRunner()
    engine = _engine(layout, transport, runner)

    engine.start(release.files)

    assert engine.status().phase == "ready"
    assert transport.count("row-bot-assets") == 0
    assert transport.count("row-bot-pylock") == 0
    assert transport.count("pythonhosted") == 0
    assert "pip" not in runner.labels()
    staged = layout.versions_dir / "5.2.1"
    reused = staged / "app" / "static" / "buddy.json"
    assert reused.read_bytes() == (app / "static" / "buddy.json").read_bytes()
    assert reused.stat().st_ino == (app / "static" / "buddy.json").stat().st_ino


def test_a_failed_self_test_removes_the_staging_folder_and_leaves_state_untouched(tmp_path):
    layout = make_install(tmp_path)
    state_before = _state_bytes(layout)
    release = make_release()
    engine = _engine(layout, FakeTransport(serve(release)), FakeRunner(fail="server_import"))

    engine.start(release.files)

    status = engine.status()
    assert status.phase == "failed"
    assert status.error == "self_test" and status.retryable
    assert not (layout.versions_dir / "5.2.1.staging").exists()
    assert not (layout.versions_dir / "5.2.1").exists()
    assert _state_bytes(layout) == state_before


def test_an_interrupted_download_resumes_with_a_range_request(tmp_path):
    layout = make_install(tmp_path)
    release = make_release()
    transport = FakeTransport(serve(release))
    code_url = release.files.files[f"row-bot-code-{release.version}.tar.xz"][0]
    transport.drop_after[code_url] = 100
    engine = _engine(layout, transport)

    engine.start(release.files)

    assert engine.status().phase == "ready"
    ranges = [rng for url, rng in transport.requests if url == code_url]
    assert ranges == [None, "bytes=100-"]


def test_a_download_failure_is_retryable_and_keeps_what_was_downloaded(tmp_path):
    layout = make_install(tmp_path)
    release = make_release()
    transport = FakeTransport(serve(release))
    pylock_url = release.files.files[f"row-bot-pylock-{release.version}.toml"][0]
    engine = _engine(layout, transport)

    # The lock export is unreachable: every attempt fails with a server error.
    original = transport.open

    def unavailable(url, *, headers, timeout):
        if url == pylock_url:
            raise DownloadError("HTTP 503", retryable=True)
        return original(url, headers=headers, timeout=timeout)

    transport.open = unavailable
    engine.start(release.files)
    status = engine.status()
    assert status.phase == "failed" and status.error == "download" and status.retryable
    work = layout.root / ".updates" / release.version
    assert (work / f"row-bot-code-{release.version}.tar.xz").is_file()

    transport.open = original
    code_requests = transport.count("row-bot-code")
    assert engine.start(release.files)
    assert engine.status().phase == "ready"
    # The code archive was already complete: reused, not fetched again.
    assert transport.count("row-bot-code") == code_requests


def test_a_tampered_part_is_rejected_and_its_download_discarded(tmp_path):
    layout = make_install(tmp_path)
    release = make_release()
    served = serve(release)
    code_url = release.files.files[f"row-bot-code-{release.version}.tar.xz"][0]
    served[code_url] = b"X" * len(served[code_url])
    engine = _engine(layout, FakeTransport(served))

    engine.start(release.files)

    status = engine.status()
    assert status.phase == "failed" and status.error == "integrity"
    assert not (layout.root / ".updates" / release.version).exists()
    assert install_layout.read_state(layout.root).ready is None


def test_a_new_python_or_a_library_without_wheels_needs_the_full_installer(tmp_path):
    layout = make_install(tmp_path)
    for release in (make_release(python="3.13.2"), make_release(no_wheel=["alpha"])):
        engine = _engine(layout, FakeTransport(serve(release)))
        engine.start(release.files)
        status = engine.status()
        assert status.phase == "failed" and status.error == "full_installer"
        assert status.retryable is False
        assert status.full_installer_url.startswith("https://github.com/")
        assert not (layout.versions_dir / release.version).exists()


def test_ready_survives_a_restart_and_restart_now_switches_and_relaunches(tmp_path):
    layout = make_install(tmp_path)
    release = make_release()
    _engine(layout, FakeTransport(serve(release))).start(release.files)

    relaunches: list[bool] = []
    busy = ["An agent is working"]
    engine = _engine(layout, FakeTransport({}), activity=lambda: list(busy), relaunches=relaunches)
    assert engine.status().phase == "ready" and engine.status().version == "5.2.1"

    with pytest.raises(EngineError) as refused:
        engine.apply(when="now")
    assert refused.value.code == "update_restart_busy"
    assert refused.value.detail == busy
    assert install_layout.read_state(layout.root).active == "5.2.0"

    engine.apply(when="now", force=True)
    assert relaunches == [True]
    state = install_layout.read_state(layout.root)
    assert (state.active, state.previous, state.ready) == ("5.2.1", "5.2.0", None)
    assert state.pending_verification is True and state.unverified_launches == 0
    assert engine.status().phase == "applying"

    # The new version starts: the engine there reports it verified.
    new_layout = InstallLayout(layout.kind, layout.root, "5.2.1", layout.versions_dir / "5.2.1")
    after = _engine(new_layout, FakeTransport({}))
    assert after.status().phase == "verified"
    assert after.status().version == "5.2.1"


def test_when_idle_waits_for_running_work(tmp_path):
    layout = make_install(tmp_path)
    release = make_release()
    _engine(layout, FakeTransport(serve(release))).start(release.files)
    busy = ["A workflow is running"]
    relaunches: list[bool] = []
    engine = _engine(layout, FakeTransport({}), activity=lambda: list(busy), relaunches=relaunches)

    assert engine.apply(when="when_idle").apply_when == "when_idle"
    assert engine.apply_when_idle_once() is True
    assert relaunches == []
    busy.clear()
    assert engine.apply_when_idle_once() is False
    assert relaunches == [True]
    assert install_layout.read_state(layout.root).active == "5.2.1"


def test_quitting_applies_a_ready_update(tmp_path):
    layout = make_install(tmp_path)
    release = make_release()
    engine = _engine(layout, FakeTransport(serve(release)))
    engine.start(release.files)
    engine.apply(when="on_quit")
    assert install_layout.read_state(layout.root).active == "5.2.0"

    assert switcher.apply_on_quit(layout) == "5.2.1"
    state = install_layout.read_state(layout.root)
    assert (state.active, state.previous, state.pending_verification) == ("5.2.1", "5.2.0", True)
    # Nothing is ready any more: a second quit changes nothing.
    assert switcher.apply_on_quit(layout) is None


def test_a_rolled_back_update_is_reported_after_the_restart(tmp_path):
    layout = make_install(tmp_path)
    release = make_release()
    engine = _engine(layout, FakeTransport(serve(release)))
    engine.start(release.files)
    engine.apply(when="now", force=True)
    # The native launcher gave up on 5.2.1 after two failed starts.
    _launcher_gives_up(layout)

    again = _engine(layout, FakeTransport({}))
    status = again.status()
    assert status.phase == "failed" and status.error == "rolled_back"


def test_an_interrupted_staging_is_cleaned_up_at_the_next_start(tmp_path):
    layout = make_install(tmp_path)
    work = layout.root / ".updates"
    work.mkdir()
    (work / "engine.json").write_text(json.dumps({"schema": 1, "phase": "staging", "version": "5.2.1"}), encoding="utf-8")
    leftover = layout.versions_dir / "5.2.1.staging"
    (leftover / "python").mkdir(parents=True)

    engine = _engine(layout, FakeTransport({}))

    assert engine.status().phase == "idle"
    assert not leftover.exists()


def test_cancel_stops_between_reads_and_keeps_the_partial_download(tmp_path):
    layout = make_install(tmp_path)
    release = make_release()
    transport = FakeTransport(serve(release))
    engine = _engine(layout, transport)
    original = transport.open

    def open_and_cancel(url, *, headers, timeout):
        response = original(url, headers=headers, timeout=timeout)
        if "row-bot-code" in url:
            engine.cancel()
        return response

    transport.open = open_and_cancel
    engine.start(release.files)

    assert engine.status().phase == "idle"
    assert install_layout.read_state(layout.root).ready is None
    assert (layout.root / ".updates" / release.version / "update.json").is_file()


def test_after_a_verified_start_old_versions_go_but_previous_stays(tmp_path):
    layout = make_install(tmp_path)
    from tests.fixtures.updates import make_version

    make_version(layout.versions_dir, "5.1.0", dists={"pip": "26.0"})
    make_version(layout.versions_dir, "5.2.1", dists={"pip": "26.0"})
    state = install_layout.read_state(layout.root)
    state.active, state.previous = "5.2.1", "5.2.0"
    install_layout.write_state(layout.root, state)
    running = InstallLayout(layout.kind, layout.root, "5.2.1", layout.versions_dir / "5.2.1")
    (layout.root / ".updates" / "5.1.9").mkdir(parents=True)

    engine = _engine(running, FakeTransport({}))
    assert engine.cleanup_after_verified_start(wait=1, poll=0) == ["5.1.0"]
    assert sorted(path.name for path in layout.versions_dir.iterdir()) == ["5.2.0", "5.2.1"]
    assert not (layout.root / ".updates" / "5.1.9").exists()

    # While the start still waits for verification nothing is deleted.
    make_version(layout.versions_dir, "5.0.0", dists={"pip": "26.0"})
    state.pending_verification = True
    install_layout.write_state(layout.root, state)
    assert engine.cleanup_after_verified_start(wait=0) == []
    assert (layout.versions_dir / "5.0.0").exists()


def test_a_version_folder_name_from_a_release_cannot_escape_the_install(tmp_path):
    layout = make_install(tmp_path)
    release = make_release()
    bad = release.files.__class__(**{**release.files.__dict__, "version": "../evil"})
    engine = _engine(layout, FakeTransport(serve(release)))
    with pytest.raises(EngineError):
        engine.start(bad)
    assert not (tmp_path / "evil").exists()


@pytest.mark.skipif(os.name == "nt", reason="the Linux current link needs symlinks")
def test_linux_switch_moves_the_current_link(tmp_path):
    from row_bot.install_layout import InstallKind

    layout = make_install(tmp_path, kind=InstallKind.LINUX)
    install_layout.set_linux_current(layout.root, "5.2.0")
    release = make_release()
    engine = _engine(layout, FakeTransport(serve(release)))
    engine.start(release.files)
    engine.apply(when="now", force=True)
    assert install_layout.linux_current_version(layout.root) == "5.2.1"


def _launcher_gives_up(layout) -> None:
    """The launcher's start after the switched-to version failed to start twice.

    ``install_layout begin-launch`` is what the Linux wrapper runs (the Windows
    launcher and the macOS host apply the same rule): back to ``previous``,
    and on Linux the ``current`` link moves back too.
    """
    state = install_layout.load_state(layout.root, layout.kind)
    state.unverified_launches = install_layout.MAX_UNVERIFIED_LAUNCHES
    install_layout.write_state(layout.root, state)
    command = ["begin-launch", "--root", str(layout.root), "--kind", layout.kind.value]
    assert install_layout.main(command) == 0
    after = install_layout.load_state(layout.root, layout.kind)
    assert after.active == layout.version and after.last_rollback["from"] != layout.version


def _saved_phase(layout) -> dict:
    return json.loads((layout.root / ".updates" / "engine.json").read_text(encoding="utf-8"))


def test_a_rollback_after_quitting_into_an_update_is_reported_once_and_held_back(tmp_path):
    layout = make_install(tmp_path)
    release = make_release()
    engine = _engine(layout, FakeTransport(serve(release)))
    engine.start(release.files)
    engine.apply(when="on_quit")
    # The launcher switches while Row-Bot quits; the engine never sees "applying".
    assert switcher.apply_on_quit(layout) == "5.2.1"
    state = install_layout.read_state(layout.root)
    assert (state.extra["switched_from"], state.extra["switched_to"]) == ("5.2.0", "5.2.1")
    assert state.extra["switched_at"]
    _launcher_gives_up(layout)

    after = _engine(layout, FakeTransport({}))
    status = after.status()
    assert (status.phase, status.version, status.error, status.retryable) == ("failed", "5.2.1", "rolled_back", False)
    assert _saved_phase(layout)["phase"] == "failed"  # reconciled and saved, not only shown
    assert switcher.held_back(layout) == ("5.2.1", "rolled_back")

    # Seen (a new check acknowledges it): not reported again at the next start.
    after.acknowledge()
    assert _engine(layout, FakeTransport({})).status().phase == "idle"
    # An explicit Download may still prepare it again.
    again = _engine(layout, FakeTransport(serve(release)))
    assert again.start(release.files) and again.status().phase == "ready"


def test_after_repair_the_version_left_is_offered_again_without_downloading(tmp_path):
    layout = make_install(tmp_path)
    release = make_release()
    _engine(layout, FakeTransport(serve(release))).start(release.files)
    _engine(layout, FakeTransport({})).apply(when="now", force=True)
    newer = InstallLayout(layout.kind, layout.root, "5.2.1", layout.versions_dir / "5.2.1")
    _engine(newer, FakeTransport({})).rollback()  # Repair: back to 5.2.0
    assert switcher.held_back(layout) == ("5.2.1", "repair")

    nothing = FakeTransport({})
    engine = _engine(layout, nothing)
    assert engine.start(release.files) is True
    status = engine.status()
    assert (status.phase, status.version) == ("ready", "5.2.1")
    assert "still installed" in (status.message or "")
    assert nothing.requests == []  # not one byte downloaded
    assert install_layout.read_state(layout.root).ready["version"] == "5.2.1"
    engine.apply(when="now", force=True)
    state = install_layout.read_state(layout.root)
    assert (state.active, state.previous) == ("5.2.1", "5.2.0")


def test_the_active_version_is_refused_before_anything_is_downloaded(tmp_path):
    layout = make_install(tmp_path)
    state = install_layout.read_state(layout.root)
    state.active = "5.2.1"  # switched by another Row-Bot; this one still runs 5.2.0
    install_layout.write_state(layout.root, state)
    from tests.fixtures.updates import make_version

    make_version(layout.versions_dir, "5.2.1", dists={"pip": "26.0"})
    release = make_release()
    transport = FakeTransport(serve(release))
    engine = _engine(layout, transport)
    assert engine.start(release.files) is False
    assert (engine.status().phase, engine.status().error) == ("failed", "installed")
    assert transport.requests == []


def test_a_stale_applying_state_is_reset_and_saved(tmp_path):
    layout = make_install(tmp_path)
    work = layout.root / ".updates"
    work.mkdir()
    # Applying 5.2.1, but the install neither runs nor starts it (an installer
    # put 5.2.0 back): nothing is being applied any more.
    (work / "engine.json").write_text(
        json.dumps({"schema": 1, "phase": "applying", "version": "5.2.1", "apply_when": "now"}), encoding="utf-8")
    assert _engine(layout, FakeTransport({})).status().phase == "idle"
    assert _saved_phase(layout)["phase"] == "idle"

    # Applying the version that now runs: verified, and saved as such.
    (work / "engine.json").write_text(
        json.dumps({"schema": 1, "phase": "applying", "version": "5.2.0", "apply_when": "now"}), encoding="utf-8")
    assert _engine(layout, FakeTransport({})).status().phase == "verified"
    assert _saved_phase(layout)["phase"] == "verified"


def test_an_interrupted_staging_is_left_alone_while_another_row_bot_holds_the_install(tmp_path):
    layout = make_install(tmp_path)
    work = layout.root / ".updates"
    work.mkdir()
    (work / "engine.json").write_text(json.dumps({"schema": 1, "phase": "staging", "version": "5.2.1"}), encoding="utf-8")
    staging = layout.versions_dir / "5.2.1.staging"
    (staging / "python").mkdir(parents=True)
    other = switcher.InstallLock.for_install(layout.root)
    assert other.acquire()
    try:
        assert _engine(layout, FakeTransport({})).status().phase == "idle"
        assert staging.is_dir()  # the other Row-Bot may be staging it right now
    finally:
        other.release()


def test_nothing_newer_is_prepared_while_an_update_is_ready(tmp_path):
    layout = make_install(tmp_path)
    first = make_release()
    engine = _engine(layout, FakeTransport(serve(first)))
    engine.start(first.files)
    newer = make_release("5.2.2")
    transport = FakeTransport(serve(newer))
    engine = _engine(layout, transport)
    assert engine.start(newer.files) is False
    assert (engine.status().phase, engine.status().version) == ("ready", "5.2.1")
    assert transport.requests == []


def test_restart_now_waits_while_an_update_is_being_prepared(tmp_path):
    layout = make_install(tmp_path)
    release = make_release()
    transport = FakeTransport(serve(release))
    opened = threading.Event()
    go_on = threading.Event()
    original = transport.open

    def slow_open(url, *, headers, timeout):
        if "row-bot-code" in url:
            opened.set()
            go_on.wait(10)
        return original(url, headers=headers, timeout=timeout)

    transport.open = slow_open
    engine = _engine(layout, transport, background=True)
    assert engine.start(release.files)
    assert opened.wait(10)
    try:
        with pytest.raises(EngineError) as refused:
            engine.apply(when="now", force=True)
        assert refused.value.code == "update_busy"
    finally:
        go_on.set()
        engine.wait(30)
    assert engine.status().phase == "ready"


def test_restart_now_and_repair_mark_the_next_start_as_row_bots_own(tmp_path):
    """Goals carry on by themselves only after a restart Row-Bot made (D4)."""
    layout = make_install(tmp_path)
    release = make_release()
    _engine(layout, FakeTransport(serve(release))).start(release.files)
    _engine(layout, FakeTransport({})).apply(when="now", force=True)
    newer = InstallLayout(layout.kind, layout.root, "5.2.1", layout.versions_dir / "5.2.1")
    assert switcher.consume_restart_reason(newer) == "update"
    assert switcher.consume_restart_reason(newer) is None  # read once
    _engine(newer, FakeTransport({})).rollback()
    assert switcher.consume_restart_reason(layout) == "repair"


def test_a_relaunch_that_does_not_happen_leaves_a_normal_start(tmp_path):
    layout = make_install(tmp_path)
    release = make_release()
    _engine(layout, FakeTransport(serve(release))).start(release.files)
    engine = _engine(layout, FakeTransport({}), relaunch=lambda: False)
    assert engine.apply(when="now", force=True).message == "Restart Row-Bot to finish updating."
    newer = InstallLayout(layout.kind, layout.root, "5.2.1", layout.versions_dir / "5.2.1")
    assert switcher.consume_restart_reason(newer) is None  # the person restarted it
