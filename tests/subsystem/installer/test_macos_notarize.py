"""Asynchronous notarization: submit early, wait a bounded time, resume later (D3).

`scripts/macos_notarize.py` drives `notarytool` and `stapler` for release.yml
and notarize-finish.yml. These tests replace both with fakes: no Apple account,
no network, no macOS.
"""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

import pytest

import scripts.macos_notarize as notarize

pytestmark = [pytest.mark.subsystem, pytest.mark.installer]

SUBMISSION = "2efe2717-52ef-43a5-96dc-0797e4ca1041"


class FakeApple:
    """notarytool and stapler: answers from scripted statuses, records each call."""

    def __init__(self, statuses=("In Progress", "Accepted"), *, submit_failures=0, staple_failures=0):
        self.statuses = list(statuses)
        self.submit_failures = submit_failures
        self.staple_failures = staple_failures
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv)
        tool = argv[1]
        if tool == "notarytool" and argv[2] == "submit":
            if self.submit_failures:
                self.submit_failures -= 1
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="HTTP 500")
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"id": SUBMISSION}), stderr="")
        if tool == "notarytool" and argv[2] == "info":
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps({"status": status}), stderr="")
        if tool == "notarytool" and argv[2] == "log":
            return subprocess.CompletedProcess(argv, 0, stdout='{"issues": ["unsigned binary"]}', stderr="")
        if tool == "stapler" and argv[2] == "staple" and self.staple_failures:
            self.staple_failures -= 1
            return subprocess.CompletedProcess(argv, 65, stdout="", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture(autouse=True)
def apple_credentials(monkeypatch):
    monkeypatch.setenv("APPLE_ID", "release@example.com")
    monkeypatch.setenv("APPLE_APP_PASSWORD", "app-password")
    monkeypatch.setenv("APPLE_TEAM_ID", "TEAMID1234")


def _submitted(tmp_path, apple):
    state = tmp_path / "notarization.json"
    notarize.save_state(state, {"schema": 1, "version": "5.2.0", "dry_run": False})
    notarize.submit(state, "app", tmp_path / "Row-Bot.app.zip", run=apple, sleep=lambda _s: None)
    return state


def test_submit_does_not_wait_and_records_the_submission(tmp_path) -> None:
    apple = FakeApple(submit_failures=1)

    state = _submitted(tmp_path, apple)

    submit_call = apple.calls[-1]
    assert submit_call[:4] == ["xcrun", "notarytool", "submit", str(tmp_path / "Row-Bot.app.zip")]
    assert "--no-wait" in submit_call
    entry = notarize.load_state(state)["app"]
    assert (entry["submission_id"], entry["status"], entry["stapled"]) == (SUBMISSION, "In Progress", False)


def test_a_bounded_wait_ends_accepted_or_pending(tmp_path) -> None:
    clock = Clock()
    state = _submitted(tmp_path, FakeApple())

    assert notarize.wait(state, "app", 10, run=FakeApple(["In Progress", "Accepted"]), sleep=clock.sleep, clock=clock) == "accepted"
    assert notarize.load_state(state)["app"]["status"] == "Accepted"

    clock = Clock()
    slow = FakeApple(["In Progress"])
    assert notarize.wait(state, "app", 10, run=slow, sleep=clock.sleep, clock=clock) == "pending"
    assert clock.now == 10 * 60
    assert len([call for call in slow.calls if call[2] == "info"]) == 10 * 60 // notarize.POLL_SECONDS + 1


def test_a_refused_submission_prints_apples_log_and_never_the_password(tmp_path, capsys) -> None:
    clock = Clock()
    state = _submitted(tmp_path, FakeApple())

    assert notarize.wait(state, "app", 10, run=FakeApple(["Invalid"]), sleep=clock.sleep, clock=clock) == "invalid"
    output = capsys.readouterr().out
    assert "unsigned binary" in output
    assert "app-password" not in output
    assert notarize.readiness(notarize.load_state(state)) == "failed"


def test_staple_retries_while_apple_publishes_the_ticket(tmp_path) -> None:
    state = _submitted(tmp_path, FakeApple())
    apple = FakeApple(staple_failures=2)

    notarize.staple(state, "app", tmp_path / "Row-Bot.app", run=apple, sleep=lambda _s: None)

    assert [call[2] for call in apple.calls] == ["staple", "staple", "staple", "validate"]
    assert notarize.load_state(state)["app"]["stapled"] is True

    with pytest.raises(notarize.NotarizeError, match="could not be stapled"):
        notarize.staple(state, "app", tmp_path / "Row-Bot.app", run=FakeApple(staple_failures=9), sleep=lambda _s: None)


@pytest.mark.parametrize(
    ("state", "readiness"),
    [
        ({"app": {"stapled": True}, "dmg": {"stapled": True}}, "ready"),
        ({"app": {"stapled": True}, "dmg": {"skipped": "dry run"}}, "app-only"),
        ({"app": {"stapled": True, "submission_id": SUBMISSION}, "dmg": {"status": "In Progress"}}, "pending"),
        ({"app": {"status": "In Progress", "submission_id": SUBMISSION}}, "pending"),
        ({}, "not-submitted"),
    ],
)
def test_readiness_says_whether_the_release_can_publish(state, readiness) -> None:
    assert notarize.readiness({"schema": 1, "version": "5.2.0", **state}) == readiness
    assert "macOS notarization of 5.2.0" in notarize.summary({"schema": 1, "version": "5.2.0", **state})


def test_a_pending_summary_names_the_artifact_notarize_finish_picks_up() -> None:
    text = notarize.summary({"schema": 1, "version": "5.2.0", "dry_run": False, "release_run_id": "77",
                             "app": {"status": "In Progress", "submission_id": SUBMISSION}})

    assert "macos-notarization-pending-5.2.0-r77" in text and "notarize-finish.yml" in text


def _run(run_id, workflow="release.yml", *, conclusion="success", created="2026-10-11T10:00:00Z",
         event="workflow_dispatch", branch="v5.2.0"):
    return {"id": run_id, "conclusion": conclusion, "created_at": created, "event": event,
            "path": f".github/workflows/{workflow}", "head_branch": branch}


def test_find_pending_lists_recent_pending_artifacts_of_trusted_runs() -> None:
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    runs = {
        "release.yml": [
            _run(1),
            _run(2, branch="feat/release-pipeline"),
            _run(3, conclusion="failure"),
            _run(4, created="2026-09-01T12:00:00Z"),
            _run(6, branch="main"),
        ],
        # A failed finish run still holds the other items' new state.
        "notarize-finish.yml": [_run(5, "notarize-finish.yml", conclusion="failure", event="schedule", branch="main")],
    }
    artifacts = {
        1: [{"id": 11, "name": "macos-notarization-pending-5.2.0-r1", "expired": False},
            {"id": 12, "name": "Row-Bot-Linux", "expired": False}],
        2: [{"id": 21, "name": "macos-notarization-pending-5.1.0-r2-dry-run", "expired": False}],
        3: [{"id": 31, "name": "macos-notarization-pending-5.2.0-r3", "expired": False}],
        5: [{"id": 51, "name": "macos-notarization-pending-5.2.0-rc.1-r9", "expired": False}],
        6: [{"id": 61, "name": "macos-notarization-pending-5.2.0-r6", "expired": False}],
        7: [{"id": 71, "name": "macos-notarization-pending-5.2.0-r7", "expired": False}],
    }
    single = {2: _run(2, branch="feat/release-pipeline"), 7: _run(7, "ci.yml", event="pull_request")}

    def gh(args):
        path = args[1]
        for workflow, listed in runs.items():
            if f"workflows/{workflow}/runs" in path:
                return {"workflow_runs": listed}
        run_id = int(path.split("/runs/")[1].split("/")[0])
        if path.endswith("/artifacts?per_page=100"):
            return {"artifacts": artifacts.get(run_id, [])}
        return single[run_id]

    found = notarize.find_pending("owner/row-bot", gh=gh, now=now)
    # Run 6 built main, not the tag: its DMG never goes on a release.
    assert [(item["run_id"], item["version"], item["release_run_id"], item["dry_run"]) for item in found] == [
        ("1", "5.2.0", "1", False), ("5", "5.2.0-rc.1", "9", False)]

    # Asked for one run, a dry run is finished too.
    assert notarize.find_pending("owner/row-bot", run_id="2", gh=gh, now=now) == [
        {"run_id": "2", "artifact_id": "21", "artifact": "macos-notarization-pending-5.1.0-r2-dry-run",
         "version": "5.1.0", "release_run_id": "2", "dry_run": True}]
    # Never the artifacts of another workflow or of pull request code.
    assert notarize.find_pending("owner/row-bot", run_id="7", gh=gh, now=now) == []


class FakeGitHub:
    """The API answers preflight and attach read: the release, the run, the tag, the newest runs."""

    def __init__(self, *, releases=None, run=None, tag_sha="a" * 40, newest=(41,), assets=()):
        self.releases = releases if releases is not None else [
            {"id": 900, "tag_name": "v5.2.0", "draft": True, "assets": list(assets)}]
        self.run = {"id": 41, "path": ".github/workflows/release.yml", "event": "workflow_dispatch",
                    "head_branch": "v5.2.0", "conclusion": "success", "head_sha": "a" * 40, **(run or {})}
        self.tag_sha = tag_sha
        self.newest = newest
        self.uploaded: list[str] = []

    def __call__(self, args):
        path = args[1]
        if path.endswith("/releases?per_page=100"):
            return self.releases
        if "/releases/900/assets" in path:
            return self.releases[0]["assets"]
        if "/commits/v5.2.0" in path:
            return {"sha": self.tag_sha}
        if "workflows/release.yml/runs?branch=v5.2.0" in path:
            return {"workflow_runs": [{"id": run_id, "display_title": "Release 5.2.0"} for run_id in self.newest]}
        if "/actions/runs/41" in path:
            return self.run
        raise AssertionError(path)

    def upload(self, tag, path):
        self.uploaded.append(tag)
        self.releases[0]["assets"].append({"name": path.name, "digest": "sha256:" + notarize._sha256(path)})


@pytest.mark.parametrize(
    ("github", "action", "reason"),
    [
        (FakeGitHub(), "finish", "is the release of v5.2.0"),
        (FakeGitHub(releases=[]), "discard", "no release v5.2.0"),
        (FakeGitHub(releases=[{"id": 900, "tag_name": "v5.2.0", "draft": False}]), "discard", "already published"),
        (FakeGitHub(releases=[{"id": 1, "tag_name": "v5.2.0", "draft": True}] * 2), "hold", "2 releases"),
        (FakeGitHub(tag_sha="b" * 40), "discard", "v5.2.0 is now"),
        (FakeGitHub(newest=(41, 57)), "discard", "run 57 is a newer release"),
        (FakeGitHub(run={"head_branch": "main"}), "discard", "not the tag v5.2.0"),
        (FakeGitHub(assets=[{"name": "Row-Bot-5.2.0-macOS-arm64.dmg", "digest": "sha256:" + "c" * 64}]),
         "discard", "already has Row-Bot-5.2.0-macOS-arm64.dmg"),
    ],
)
def test_preflight_finishes_only_a_notarization_the_draft_can_still_take(github, action, reason) -> None:
    decision = notarize.preflight("5.2.0", "41", False, "owner/row-bot", github)

    assert decision["action"] == action and reason in decision["reason"]


def test_attach_never_replaces_a_dmg_and_records_the_one_it_attached(tmp_path) -> None:
    dmg = tmp_path / "Row-Bot-5.2.0-macOS-arm64.dmg"
    dmg.write_bytes(b"stapled dmg")
    github = FakeGitHub()

    assert notarize.attach(dmg, "5.2.0", "41", "owner/row-bot", record=tmp_path / "record.json",
                           gh=github, upload=github.upload) == "attached"
    record = json.loads((tmp_path / "record.json").read_text(encoding="utf-8"))
    assert (record["release_run_id"], record["name"], record["sha256"]) == ("41", dmg.name, notarize._sha256(dmg))
    # Again (a second finish run): it is already there, nothing is uploaded twice.
    assert notarize.attach(dmg, "5.2.0", "41", "owner/row-bot", record=tmp_path / "record.json",
                           gh=github, upload=github.upload) == "already"
    assert github.uploaded == ["v5.2.0"]

    # Another DMG on the draft is never replaced.
    other = FakeGitHub(assets=[{"name": dmg.name, "digest": "sha256:" + "c" * 64}])
    assert notarize.attach(dmg, "5.2.0", "41", "owner/row-bot", record=tmp_path / "other.json",
                           gh=other, upload=other.upload) == "discard"
    assert other.uploaded == [] and not (tmp_path / "other.json").exists()
    published = FakeGitHub(releases=[{"id": 900, "tag_name": "v5.2.0", "draft": False, "assets": []}])
    assert notarize.attach(dmg, "5.2.0", "41", "owner/row-bot", record=tmp_path / "late.json",
                           gh=published, upload=published.upload) == "discard"
    assert published.uploaded == []


class FakeMac:
    """codesign, stapler, hdiutil and plutil for a DMG and the app inside."""

    def __init__(self, *, team="33PQRTZLNW", app_team="33PQRTZLNW", version="5.2.0", stapled=True):
        self.team, self.app_team, self.version, self.stapled = team, app_team, version, stapled
        self.calls: list[list[str]] = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv)
        out = ""
        err = ""
        code = 0
        if argv[0] == "codesign" and argv[1] == "-dv":
            err = f"Identifier=x\nTeamIdentifier={self.app_team if argv[-1].endswith('.app') else self.team}\n"
        elif argv[:2] == ["xcrun", "stapler"]:
            code = 0 if self.stapled else 65
        elif argv[0] == "plutil":
            out = self.version + "\n"
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr=err)


@pytest.mark.parametrize(
    ("mac", "problem"),
    [
        (FakeMac(), None),
        (FakeMac(team="OTHERTEAM1"), "is not signed by team 33PQRTZLNW"),
        (FakeMac(app_team="OTHERTEAM1"), "the app in"),
        (FakeMac(version="5.1.0"), "is 5.1.0, not 5.2.0"),
        (FakeMac(stapled=False), "no valid stapled ticket"),
    ],
)
def test_a_dmg_is_checked_before_it_is_attached(tmp_path, mac, problem) -> None:
    problems = notarize.verify_dmg(tmp_path / "Row-Bot-5.2.0-macOS-arm64.dmg", "5.2.0", notarized=True, run=mac)

    assert problems == [] if problem is None else any(problem in item for item in problems)
    assert mac.calls[-1][:2] == ["hdiutil", "detach"]


def test_a_tool_that_gives_no_answer_counts_as_no_answer(tmp_path) -> None:
    state = _submitted(tmp_path, FakeApple())

    def silent(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    clock = Clock()
    assert notarize.wait(state, "app", 1, run=silent, sleep=clock.sleep, clock=clock) == "pending"


def _release_job(tmp_path, apple):
    """The state right after release.yml submitted the app."""
    folder = tmp_path / "notarization"
    folder.mkdir()
    (folder / "Row-Bot.app.zip").write_bytes(b"zip")
    state = folder / "notarization.json"
    notarize.save_state(state, {"schema": 1, "version": "5.2.0", "dry_run": False})
    notarize.submit(state, "app", folder / "Row-Bot.app.zip", run=apple, sleep=lambda _s: None)
    app = tmp_path / "build" / "Row-Bot.app"
    (app / "Contents").mkdir(parents=True)
    return state, app


def _dmg_builder(built):
    def build(app, dmg):
        built.append(app)
        dmg.parent.mkdir(parents=True, exist_ok=True)
        dmg.write_bytes(b"dmg")
    return build


def test_finish_staples_the_app_then_notarizes_and_staples_the_dmg(tmp_path) -> None:
    apple, clock, built = FakeApple(["Accepted"]), Clock(), []
    state, app = _release_job(tmp_path, apple)
    dmg = tmp_path / "dist" / "Row-Bot-5.2.0-macOS-arm64.dmg"

    result = notarize.finish(state, app=app, dmg=dmg, minutes=30, dry_run=False, build_dmg=_dmg_builder(built),
                             run=apple, sleep=clock.sleep, clock=clock)

    assert result == "ready" and built == [app]
    steps = [(call[1], call[2], call[3]) for call in apple.calls if call[2] in {"submit", "staple"}]
    assert steps == [("notarytool", "submit", str(state.parent / "Row-Bot.app.zip")),
                     ("stapler", "staple", str(app)),
                     ("notarytool", "submit", str(dmg)),
                     ("stapler", "staple", str(dmg))]
    assert notarize.readiness(notarize.load_state(state)) == "ready"
    # The submitted zip is not needed once the app is stapled.
    assert not (state.parent / "Row-Bot.app.zip").exists()


def test_a_dry_run_notarizes_the_app_once_and_signs_the_dmg_only(tmp_path) -> None:
    apple, clock, built = FakeApple(["Accepted"]), Clock(), []
    state, app = _release_job(tmp_path, apple)

    result = notarize.finish(state, app=app, dmg=tmp_path / "dist" / "x.dmg", minutes=30, dry_run=True,
                             build_dmg=_dmg_builder(built), run=apple, sleep=clock.sleep, clock=clock)

    assert result == "app-only"
    assert len([call for call in apple.calls if call[2] == "submit"]) == 1
    assert notarize.load_state(state)["dmg"] == {"skipped": "dry run (one submission per dry run)"}


def test_both_waits_end_before_the_job_does(tmp_path) -> None:
    clock, built = Clock(), []
    state, app = _release_job(tmp_path, FakeApple())
    slow = FakeApple(["In Progress"])

    # 3 minutes left of the job: one wait of at most 3 minutes, not 45.
    result = notarize.finish(state, app=app, dmg=tmp_path / "x.dmg", minutes=45, dry_run=False,
                             build_dmg=_dmg_builder(built), run=slow, sleep=clock.sleep, clock=clock,
                             until=1000 + 180, epoch=lambda: 1000)

    assert result == "pending" and clock.now == 180


def test_finish_resumes_where_the_release_job_ran_out_of_time(tmp_path) -> None:
    clock, built = Clock(), []
    state, app = _release_job(tmp_path, FakeApple())
    dmg = tmp_path / "dist" / "Row-Bot-5.2.0-macOS-arm64.dmg"

    # The release job: Apple accepts the app but is still checking the DMG.
    slow_dmg = FakeApple(["Accepted", "In Progress"])
    assert notarize.finish(state, app=app, dmg=dmg, minutes=5, dry_run=False, build_dmg=_dmg_builder(built),
                           run=slow_dmg, sleep=clock.sleep, clock=clock) == "pending"
    kept = state.parent / dmg.name
    assert kept.is_file() and not dmg.exists()
    assert notarize.readiness(notarize.load_state(state)) == "pending"

    # notarize-finish.yml, later, with the pending artifact: it only waits and staples.
    later = FakeApple(["Accepted"])
    assert notarize.finish(state, app=tmp_path / "unused" / "Row-Bot.app", dmg=kept, minutes=5, dry_run=False,
                           build_dmg=_dmg_builder(built), run=later, sleep=clock.sleep, clock=clock) == "ready"
    assert len(built) == 1
    assert [call[2] for call in later.calls if call[1] == "stapler"] == ["staple", "validate"]


def test_finish_unpacks_the_submitted_app_when_the_app_itself_was_pending(tmp_path) -> None:
    clock, built = Clock(), []
    state, _ = _release_job(tmp_path, FakeApple())
    later = FakeApple(["Accepted"])
    app = state.parent / "app" / "Row-Bot.app"

    assert notarize.finish(state, app=app, dmg=state.parent / "x.dmg", minutes=5, dry_run=False,
                           build_dmg=_dmg_builder(built), run=later, sleep=clock.sleep, clock=clock) == "ready"
    assert later.calls[1][:3] == ["ditto", "-x", "-k"]
    assert built == [app]
