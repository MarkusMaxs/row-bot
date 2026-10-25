"""Asynchronous macOS notarization for release.yml and notarize-finish.yml (D3).

The release submits the signed app as soon as it is built and does other work
while Apple checks it. It then waits, for a bounded time, staples the app,
builds and signs the DMG from it, and notarizes and staples the DMG the same
way. When a wait runs out, the job uploads what it has and the submission ids
as a ``macos-notarization-pending-<version>-r<release run>`` artifact;
``notarize-finish.yml`` picks that up later. A release's DMG is attached to
its draft only once it is stapled and only for the newest release run of the
tag's current commit, so a DMG on the draft means macOS is ready.

State (``notarization.json``, next to the files it describes)::

    {"schema": 1, "version": "5.2.0", "dry_run": false, "release_run_id": "123",
     "app": {"file": "Row-Bot.app.zip", "submission_id": "...", "status": "Accepted",
             "submitted_at": "...", "stapled": true},
     "dmg": {"file": "Row-Bot-5.2.0-macOS-arm64.dmg", "submission_id": "...", ...}}

Apple credentials come from APPLE_ID, APPLE_APP_PASSWORD and APPLE_TEAM_ID and
are never printed. Every call to Apple's tools has a time limit; a call that
runs out counts as "no answer". Subcommands: ``submit``, ``wait``, ``staple``,
``skip``, ``finish``, ``summary``, ``find-pending``, ``preflight``,
``verify-dmg`` and ``attach``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

SCHEMA = 1
POLL_SECONDS = 30
SUBMIT_ATTEMPTS = 3
STAPLE_ATTEMPTS = 5
#: The Developer ID team every shipped app and DMG is signed by.
TEAM_ID = "33PQRTZLNW"
#: Seconds each call may take: uploading a 1.4 GB DMG, asking for a status, stapling, unpacking.
TIMEOUTS = {"submit": 45 * 60, "info": 120, "log": 120, "stapler": 300, "ditto": 900, "codesign": 300,
            "hdiutil": 600, "plutil": 60}
PENDING_ARTIFACT = re.compile(
    r"^macos-notarization-pending-(?P<version>[0-9A-Za-z][0-9A-Za-z.+-]*?)-r(?P<run>\d+)(?P<dry>-dry-run)?$"
)
#: Workflows whose runs may hold a pending notarization.
WORKFLOWS = ("release.yml", "notarize-finish.yml")
#: Events that run a pull request's code: their artifacts are never trusted.
PULL_REQUEST_EVENTS = {"pull_request", "pull_request_target"}
#: notarize-finish looks back this far for pending notarizations.
LOOK_BACK_DAYS = 7

Runner = Callable[..., subprocess.CompletedProcess]
Gh = Callable[[list[str]], Any]


class NotarizeError(Exception):
    pass


def pending_artifact_name(version: str, release_run_id: str, dry_run: bool) -> str:
    return f"macos-notarization-pending-{version}-r{release_run_id}" + ("-dry-run" if dry_run else "")


def record_artifact_name(release_run_id: str) -> str:
    """notarize-finish.yml's record of the DMG it attached for a release run."""
    return f"macos-dmg-record-{release_run_id}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _call(run: Runner, argv: list[str], kind: str, **kwargs: Any) -> subprocess.CompletedProcess:
    """Run a tool with its time limit; running out is an answer of its own (exit 124)."""
    try:
        return run(argv, timeout=TIMEOUTS[kind], check=False, **kwargs)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 124, stdout="", stderr=f"{kind} gave no answer in {TIMEOUTS[kind]} s")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"schema": SCHEMA}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise NotarizeError(f"{path} is not a notarization state file")
    return data


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def _credentials() -> list[str]:
    values = {name: os.environ.get(name, "") for name in ("APPLE_ID", "APPLE_APP_PASSWORD", "APPLE_TEAM_ID")}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise NotarizeError(f"missing {', '.join(missing)}")
    return ["--apple-id", values["APPLE_ID"], "--password", values["APPLE_APP_PASSWORD"],
            "--team-id", values["APPLE_TEAM_ID"]]


def _notarytool(run: Runner, kind: str, *args: str) -> tuple[int, dict[str, Any], str]:
    completed = _call(run, ["xcrun", "notarytool", *args, *_credentials(), "--output-format", "json"], kind,
                      capture_output=True, text=True)
    try:
        payload = json.loads(completed.stdout or "{}")
    except ValueError:
        payload = {}
    return completed.returncode, payload if isinstance(payload, dict) else {}, (completed.stderr or "").strip()


def submit(state_path: Path, label: str, file: Path, *, run: Runner = subprocess.run,
           sleep: Callable[[float], None] = time.sleep) -> str:
    """Upload *file* without waiting for Apple; record the submission id."""
    state = load_state(state_path)
    for attempt in range(1, SUBMIT_ATTEMPTS + 1):
        code, payload, error = _notarytool(run, "submit", "submit", str(file), "--no-wait")
        submission = str(payload.get("id") or "")
        if code == 0 and re.fullmatch(r"[0-9a-fA-F-]{36}", submission):
            state[label] = {"file": Path(file).name, "submission_id": submission, "status": "In Progress",
                            "submitted_at": _now(), "stapled": False}
            save_state(state_path, state)
            print(f"{label}: submitted {Path(file).name} to Apple as {submission}")
            return submission
        print(f"::warning::{label}: notarization upload attempt {attempt} failed: {error or payload.get('message', '')}")
        if attempt < SUBMIT_ATTEMPTS:
            sleep(30)
    raise NotarizeError(f"{label}: Apple did not take the upload after {SUBMIT_ATTEMPTS} attempts")


def wait(state_path: Path, label: str, minutes: float, *, run: Runner = subprocess.run,
         sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> str:
    """Poll until Apple decides or *minutes* pass: ``accepted``, ``pending`` or ``invalid``."""
    state = load_state(state_path)
    entry = state.get(label) or {}
    submission = entry.get("submission_id")
    if not submission:
        raise NotarizeError(f"{label}: nothing was submitted")
    deadline = clock() + max(0.0, minutes) * 60
    status = str(entry.get("status") or "In Progress")
    while True:
        code, payload, error = _notarytool(run, "info", "info", submission)
        if code == 0 and payload.get("status"):
            status = str(payload["status"])
        else:
            print(f"::warning::{label}: Apple's status service did not answer ({error or code})")
        if status != "In Progress" or clock() >= deadline:
            break
        sleep(POLL_SECONDS)
    entry["status"] = status
    entry["checked_at"] = _now()
    state[label] = entry
    save_state(state_path, state)
    if status == "Accepted":
        print(f"{label}: Apple accepted {submission}")
        return "accepted"
    if status == "In Progress":
        print(f"::warning::{label}: Apple is still checking {submission} after {minutes:g} minutes")
        return "pending"
    print(f"::error::{label}: Apple did not accept {submission} ({status}). Its log:")
    log = _call(run, ["xcrun", "notarytool", "log", submission, *_credentials()], "log", capture_output=True, text=True)
    print(log.stdout)
    return "invalid"


def staple(state_path: Path, label: str, target: Path, *, run: Runner = subprocess.run,
           sleep: Callable[[float], None] = time.sleep) -> None:
    """Staple the ticket (Apple's servers can take minutes to serve it), then validate."""
    for attempt in range(1, STAPLE_ATTEMPTS + 1):
        if _call(run, ["xcrun", "stapler", "staple", str(target)], "stapler").returncode == 0:
            break
        if attempt == STAPLE_ATTEMPTS:
            raise NotarizeError(f"{label}: the ticket could not be stapled to {target.name}")
        sleep(30 * attempt)
    if _call(run, ["xcrun", "stapler", "validate", str(target)], "stapler").returncode != 0:
        raise NotarizeError(f"{label}: the stapled ticket of {target.name} does not validate")
    state = load_state(state_path)
    state.setdefault(label, {})["stapled"] = True
    save_state(state_path, state)
    print(f"{label}: stapled {target.name}")


def skip(state_path: Path, label: str, reason: str) -> None:
    state = load_state(state_path)
    state[label] = {"skipped": reason}
    save_state(state_path, state)


def finish(
    state_path: Path, *, app: Path, dmg: Path, minutes: float, dry_run: bool,
    build_dmg: Callable[[Path, Path], None], run: Runner = subprocess.run,
    sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic,
    until: float | None = None, epoch: Callable[[], float] = time.time,
) -> str:
    """Everything after the app's submission, resumable at any point.

    Waits for the app and staples it, builds and signs the DMG from it,
    notarizes and staples the DMG. Returns ``ready``, ``app-only`` (dry run:
    the DMG is not submitted), ``pending`` or ``failed``. On ``pending`` the
    state folder holds what a later run needs: the app's zip while the app is
    being checked, the DMG while the DMG is. *until* (seconds since the epoch)
    bounds both waits together, so the job ends in time to upload that state.
    """
    def budget() -> float:
        if until is None:
            return minutes
        return max(0.0, min(minutes, (until - epoch()) / 60))

    folder = state_path.parent
    state = load_state(state_path)
    app_entry = state.get("app") or {}
    if not app_entry.get("submission_id"):
        raise NotarizeError("app: nothing was submitted")
    if not app_entry.get("stapled"):
        status = wait(state_path, "app", budget(), run=run, sleep=sleep, clock=clock)
        if status != "accepted":
            return "pending" if status == "pending" else "failed"
        if not app.exists():
            zipped = folder / str(app_entry.get("file") or "Row-Bot.app.zip")
            app.parent.mkdir(parents=True, exist_ok=True)
            if _call(run, ["ditto", "-x", "-k", str(zipped), str(app.parent)], "ditto").returncode != 0:
                raise NotarizeError(f"could not unpack {zipped.name}")
        staple(state_path, "app", app, run=run, sleep=sleep)
        (folder / str(app_entry.get("file") or "Row-Bot.app.zip")).unlink(missing_ok=True)
    state = load_state(state_path)
    dmg_entry = state.get("dmg") or {}
    if not dmg_entry.get("submission_id") and not dmg_entry.get("skipped"):
        build_dmg(app, dmg)
        if dry_run:
            skip(state_path, "dmg", "dry run (one submission per dry run)")
            return "app-only"
        submit(state_path, "dmg", dmg, run=run, sleep=sleep)
    elif dmg_entry.get("skipped"):
        return "app-only"
    if not (load_state(state_path).get("dmg") or {}).get("stapled"):
        status = wait(state_path, "dmg", budget(), run=run, sleep=sleep, clock=clock)
        if status == "pending":
            if dmg.parent.resolve() != folder.resolve():
                os.replace(dmg, folder / dmg.name)
            return "pending"
        if status != "accepted":
            return "failed"
        staple(state_path, "dmg", dmg, run=run, sleep=sleep)
    return "ready"


def readiness(state: dict[str, Any]) -> str:
    """``ready`` (DMG stapled), ``app-only`` (dry run), ``pending``, ``failed`` or ``not-submitted``."""
    dmg, app = state.get("dmg") or {}, state.get("app") or {}
    if dmg.get("stapled"):
        return "ready"
    if dmg.get("skipped") and app.get("stapled"):
        return "app-only"
    if "Invalid" in (dmg.get("status"), app.get("status")) or "Rejected" in (dmg.get("status"), app.get("status")):
        return "failed"
    if not app.get("submission_id"):
        return "not-submitted"
    return "pending"


def summary(state: dict[str, Any]) -> str:
    version = state.get("version", "?")
    lines = [f"### macOS notarization of {version}" + (" (dry run)" if state.get("dry_run") else ""), "",
             "| Part | Submission | Apple | Stapled |", "|---|---|---|---|"]
    for label in ("app", "dmg"):
        entry = state.get(label) or {}
        if entry.get("skipped"):
            lines.append(f"| {label} | - | skipped: {entry['skipped']} | - |")
            continue
        lines.append(f"| {label} | `{entry.get('submission_id', '-')}` | {entry.get('status', 'not submitted')} | "
                     f"{'yes' if entry.get('stapled') else 'no'} |")
    status = readiness(state)
    lines.append("")
    if status == "ready":
        lines.append("macOS is ready: the stapled DMG goes to the draft release (or, in a dry run, to this run).")
    elif status == "app-only":
        lines.append("Dry run: the app is notarized and stapled; the DMG was signed but not submitted.")
    elif status == "pending":
        name = pending_artifact_name(version, str(state.get("release_run_id", "")), bool(state.get("dry_run")))
        lines.append(f"Apple is still checking. `{name}` holds the files; `notarize-finish.yml` finishes them "
                     "(hourly, or run it now with this run's id). The release cannot be published until the "
                     "stapled DMG is on the draft.")
    elif status == "not-submitted":
        lines.append("Nothing reached Apple: see the submit step. Run the release again.")
    else:
        lines.append("Apple refused the submission: see its log above. Fix the build and run the release again.")
    return "\n".join(lines) + "\n"


# ── which pending notarizations to finish ───────────────────────────────────


def _gh_json(args: list[str]) -> Any:
    completed = subprocess.run(["gh", *args], capture_output=True, text=True, check=False, timeout=120)
    if completed.returncode != 0:
        raise NotarizeError(f"gh {' '.join(args[:2])} failed: {completed.stderr.strip()}")
    return json.loads(completed.stdout or "null")


def _trusted_run(item: Mapping[str, Any], version: str, dry_run: bool) -> str | None:
    """Why a run's artifact may not be used (None: it may)."""
    path = str(item.get("path") or "")
    workflow = path.rsplit("/", 1)[-1].split("@", 1)[0]
    if workflow not in WORKFLOWS:
        return f"run {item.get('id')} is {path or 'an unknown workflow'}"
    if item.get("event") in PULL_REQUEST_EVENTS:
        return f"run {item.get('id')} ran pull request code"
    if workflow == "release.yml" and not dry_run and item.get("head_branch") != f"v{version}":
        return f"run {item.get('id')} built {item.get('head_branch')}, not v{version}"
    return None


def find_pending(repository: str, *, run_id: str | None = None, gh: Gh | None = None,
                 now: datetime | None = None) -> list[dict[str, Any]]:
    """Pending notarizations: their runs' ``macos-notarization-pending-*`` artifacts.

    Successful release runs and every completed notarize-finish run (one item
    failing must not hide the others) of the last week, or one given run (dry
    runs only then). Runs of other workflows or of pull request code are ignored.
    """
    query = gh or _gh_json
    runs: list[dict[str, Any]] = []
    if run_id:
        runs = [query(["api", f"repos/{repository}/actions/runs/{int(run_id)}"])]
    else:
        for workflow in WORKFLOWS:
            listed = query(["api", f"repos/{repository}/actions/workflows/{workflow}/runs?status=completed&per_page=50"])
            for item in (listed or {}).get("workflow_runs") or []:
                if workflow == "notarize-finish.yml" or item.get("conclusion") == "success":
                    runs.append(item)
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=LOOK_BACK_DAYS)
    found: list[dict[str, Any]] = []
    for item in runs:
        created = item.get("created_at")
        if not run_id and created and datetime.fromisoformat(str(created).replace("Z", "+00:00")) < cutoff:
            continue
        artifacts = query(["api", f"repos/{repository}/actions/runs/{item['id']}/artifacts?per_page=100"])
        for artifact in (artifacts or {}).get("artifacts") or []:
            match = PENDING_ARTIFACT.fullmatch(str(artifact.get("name") or ""))
            if not match or artifact.get("expired"):
                continue
            dry_run = bool(match.group("dry"))
            if dry_run and not run_id:
                continue
            reason = _trusted_run(item, match.group("version"), dry_run)
            if reason:
                print(f"::warning::ignoring {artifact['name']}: {reason}")
                continue
            found.append({"run_id": str(item["id"]), "artifact_id": str(artifact["id"]),
                          "artifact": artifact["name"], "version": match.group("version"),
                          "release_run_id": match.group("run"), "dry_run": dry_run})
    return found


def _releases_for(version: str, repository: str, gh: Gh) -> list[dict[str, Any]]:
    listed = gh(["api", f"repos/{repository}/releases?per_page=100"]) or []
    return [release for release in listed if release.get("tag_name") == f"v{version}"]


def release_run_problems(version: str, release_run_id: str, repository: str, gh: Gh) -> list[str]:
    """Why a release run's DMG must not go on the draft of v<version>."""
    run = gh(["api", f"repos/{repository}/actions/runs/{int(release_run_id)}"])
    problems = []
    if not str(run.get("path") or "").startswith(".github/workflows/release.yml"):
        problems.append(f"run {release_run_id} is not a release.yml run")
    if run.get("event") != "workflow_dispatch":
        problems.append(f"run {release_run_id} was started by {run.get('event')}")
    if run.get("head_branch") != f"v{version}":
        problems.append(f"run {release_run_id} built {run.get('head_branch')}, not the tag v{version}")
    if run.get("conclusion") != "success":
        problems.append(f"run {release_run_id} did not succeed")
    tag_sha = (gh(["api", f"repos/{repository}/commits/v{version}"]) or {}).get("sha")
    if run.get("head_sha") != tag_sha:
        problems.append(f"run {release_run_id} built {run.get('head_sha')}, but v{version} is now {tag_sha}")
    listed = gh(["api", f"repos/{repository}/actions/workflows/release.yml/runs?branch=v{version}&status=success&per_page=50"])
    newest = max((int(item["id"]) for item in (listed or {}).get("workflow_runs") or []
                  if item.get("display_title") == f"Release {version}"), default=None)
    if newest is not None and newest != int(release_run_id):
        problems.append(f"run {newest} is a newer release of v{version}")
    return problems


def preflight(version: str, release_run_id: str, dry_run: bool, repository: str, gh: Gh | None = None) -> dict[str, str]:
    """Before downloading anything: ``finish``, ``discard`` (never attachable) or ``hold`` (a person decides)."""
    if dry_run:
        return {"action": "finish", "reason": "dry run: the DMG stays in the run's artifacts"}
    query = gh or _gh_json
    releases = _releases_for(version, repository, query)
    if not releases:
        return {"action": "discard", "reason": f"there is no release v{version} (the draft was deleted)"}
    if len(releases) > 1:
        return {"action": "hold", "reason": f"{len(releases)} releases are tagged v{version}"}
    if not releases[0].get("draft"):
        return {"action": "discard", "reason": f"v{version} is already published"}
    dmg = f"Row-Bot-{version}-macOS-arm64.dmg"
    if any(asset.get("name") == dmg for asset in releases[0].get("assets") or []):
        return {"action": "discard", "reason": f"the draft already has {dmg}"}
    problems = release_run_problems(version, release_run_id, repository, query)
    if problems:
        return {"action": "discard", "reason": "; ".join(problems)}
    return {"action": "finish", "reason": f"run {release_run_id} is the release of v{version}"}


def verify_dmg(dmg: Path, version: str, *, notarized: bool, run: Runner = subprocess.run) -> list[str]:
    """What is wrong with a DMG before it is attached: staple, team, and the app inside."""
    problems: list[str] = []

    def team_of(target: Path) -> str | None:
        shown = _call(run, ["codesign", "-dv", "--verbose=2", str(target)], "codesign", capture_output=True, text=True)
        match = re.search(r"^TeamIdentifier=(\S+)$", (shown.stderr or "") + (shown.stdout or ""), re.MULTILINE)
        return match.group(1) if match else None

    if notarized and _call(run, ["xcrun", "stapler", "validate", str(dmg)], "stapler").returncode != 0:
        problems.append(f"{dmg.name} has no valid stapled ticket")
    if team_of(dmg) != TEAM_ID:
        problems.append(f"{dmg.name} is not signed by team {TEAM_ID}")
    with tempfile.TemporaryDirectory(prefix="row-bot-dmg-") as folder:
        mount = Path(folder) / "volume"
        mount.mkdir()
        attached = _call(run, ["hdiutil", "attach", "-nobrowse", "-readonly", "-noautoopen", "-mountpoint", str(mount),
                               str(dmg)], "hdiutil", capture_output=True, text=True)
        if attached.returncode != 0:
            reason = " ".join(((attached.stderr or "") + " " + (attached.stdout or "")).split())[:300]
            return problems + [f"{dmg.name} does not mount" + (f": {reason}" if reason else "")]
        try:
            app = mount / "Row-Bot.app"
            if team_of(app) != TEAM_ID:
                problems.append(f"the app in {dmg.name} is not signed by team {TEAM_ID}")
            if _call(run, ["codesign", "--verify", "--deep", "--strict", str(app)], "codesign").returncode != 0:
                problems.append(f"the app in {dmg.name} has a broken signature")
            if _call(run, ["xcrun", "stapler", "validate", str(app)], "stapler").returncode != 0:
                problems.append(f"the app in {dmg.name} has no valid stapled ticket")
            shown = _call(run, ["plutil", "-extract", "CFBundleShortVersionString", "raw",
                                str(app / "Contents" / "Info.plist")], "plutil", capture_output=True, text=True)
            if (shown.stdout or "").strip() != version:
                problems.append(f"the app in {dmg.name} is {(shown.stdout or '').strip() or 'unversioned'}, not {version}")
        finally:
            _call(run, ["hdiutil", "detach", str(mount)], "hdiutil", capture_output=True, text=True)
    return problems


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def attach(dmg: Path, version: str, release_run_id: str, repository: str, *, record: Path,
           gh: Gh | None = None, upload: Callable[[str, Path], None] | None = None) -> str:
    """Put a stapled DMG on the draft of v<version>, checked right before the upload.

    Never replaces a DMG already on the draft. Returns ``attached``,
    ``already`` (this DMG is there), ``discard`` (the run's DMG can never go on
    this draft) or ``hold`` (something a person must look at). Writes the
    record the publish step accepts the DMG by.
    """
    query = gh or _gh_json
    digest = _sha256(dmg)
    releases = _releases_for(version, repository, query)
    present = [asset for release in releases[:1] for asset in release.get("assets") or [] if asset.get("name") == dmg.name]
    if len(releases) == 1 and releases[0].get("draft") and present and present[0].get("digest") == f"sha256:{digest}":
        decision = {"action": "finish"}  # this very DMG is there already (an earlier finish run)
    else:
        decision = preflight(version, release_run_id, False, repository, query)
    if decision["action"] != "finish":
        print(f"::warning::not attaching {dmg.name}: {decision['reason']}")
        return decision["action"]
    release = releases[0]
    if present:
        if present[0].get("digest") == f"sha256:{digest}":
            outcome = "already"
        else:
            print(f"::error::a different {dmg.name} is already on the draft; it is not replaced")
            return "hold"
    else:
        (upload or _gh_upload)(f"v{version}", dmg)
        assets = query(["api", f"repos/{repository}/releases/{release['id']}/assets?per_page=100"]) or []
        landed = [asset for asset in assets if asset.get("name") == dmg.name]
        if not landed or (landed[0].get("digest") and landed[0]["digest"] != f"sha256:{digest}"):
            raise NotarizeError(f"{dmg.name} did not arrive on the draft intact")
        outcome = "attached"
    record.write_text(json.dumps({"schema": SCHEMA, "version": version, "release_run_id": str(release_run_id),
                                  "name": dmg.name, "sha256": digest, "size": dmg.stat().st_size}, indent=2) + "\n",
                      encoding="utf-8", newline="\n")
    return outcome


def _gh_upload(tag: str, path: Path) -> None:
    completed = subprocess.run(["gh", "release", "upload", tag, str(path)], check=False, timeout=3600)
    if completed.returncode != 0:
        raise NotarizeError(f"uploading {path.name} failed")


def _output(name: str, value: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(f"{name}={value}\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("submit")
    start.add_argument("--state", type=Path, required=True)
    start.add_argument("--label", choices=("app", "dmg"), required=True)
    start.add_argument("--file", type=Path, required=True)
    start.add_argument("--version")
    start.add_argument("--dry-run", action="store_true")
    start.add_argument("--release-run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    poll = commands.add_parser("wait")
    poll.add_argument("--state", type=Path, required=True)
    poll.add_argument("--label", choices=("app", "dmg"), required=True)
    poll.add_argument("--minutes", type=float, required=True)
    fix = commands.add_parser("staple")
    fix.add_argument("--state", type=Path, required=True)
    fix.add_argument("--label", choices=("app", "dmg"), required=True)
    fix.add_argument("--target", type=Path, required=True)
    leave = commands.add_parser("skip")
    leave.add_argument("--state", type=Path, required=True)
    leave.add_argument("--label", choices=("app", "dmg"), required=True)
    leave.add_argument("--reason", required=True)
    rest = commands.add_parser("finish", help="wait, staple the app, build the DMG, notarize and staple it")
    rest.add_argument("--state", type=Path, required=True)
    rest.add_argument("--app", type=Path, required=True, help="the app bundle (unpacked from the state's zip if missing)")
    rest.add_argument("--dmg", type=Path, required=True)
    rest.add_argument("--minutes", type=float, required=True, help="how long each wait for Apple may take")
    rest.add_argument("--until", type=float, help="seconds since the epoch by which both waits are over")
    rest.add_argument("--identity", default=os.environ.get("CODESIGN_IDENTITY", ""))
    rest.add_argument("--dry-run", action="store_true")
    report = commands.add_parser("summary")
    report.add_argument("--state", type=Path, required=True)
    scan = commands.add_parser("find-pending")
    scan.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    scan.add_argument("--run-id")
    first = commands.add_parser("preflight")
    first.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    first.add_argument("--version", required=True)
    first.add_argument("--release-run-id", required=True)
    first.add_argument("--dry-run", choices=("true", "false"), required=True)
    check = commands.add_parser("verify-dmg")
    check.add_argument("--dmg", type=Path, required=True)
    check.add_argument("--version", required=True)
    check.add_argument("--notarized", choices=("true", "false"), required=True)
    put = commands.add_parser("attach")
    put.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    put.add_argument("--dmg", type=Path, required=True)
    put.add_argument("--version", required=True)
    put.add_argument("--release-run-id", required=True)
    put.add_argument("--record", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        if args.command == "submit":
            state = load_state(args.state)
            if args.version:
                state.update(version=args.version, dry_run=bool(args.dry_run), release_run_id=str(args.release_run_id))
                save_state(args.state, state)
            submit(args.state, args.label, args.file)
        elif args.command == "wait":
            status = wait(args.state, args.label, args.minutes)
            _output("status", status)
            return 1 if status == "invalid" else 0
        elif args.command == "staple":
            staple(args.state, args.label, args.target)
        elif args.command == "skip":
            skip(args.state, args.label, args.reason)
        elif args.command == "finish":
            script = Path(__file__).resolve().parents[1] / "installer" / "build_mac_dmg.sh"

            def build_dmg(app: Path, dmg: Path) -> None:
                # The DMG's layout step installs its two pinned packages with this run's own Python.
                if subprocess.run(["bash", str(script), str(app), str(dmg), args.identity], check=False,
                                  env={**os.environ, "DMG_PYTHON": sys.executable},
                                  timeout=TIMEOUTS["hdiutil"] * 2).returncode:
                    raise NotarizeError(f"building {dmg.name} failed")

            result = finish(args.state, app=args.app, dmg=args.dmg, minutes=args.minutes,
                            dry_run=args.dry_run, build_dmg=build_dmg, until=args.until)
            _output("readiness", result)
            print(f"macOS: {result}")
            return 1 if result == "failed" else 0
        elif args.command == "summary":
            state = load_state(args.state)
            text = summary(state)
            print(text)
            _output("readiness", readiness(state))
            if os.environ.get("GITHUB_STEP_SUMMARY"):
                with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as handle:
                    handle.write(text)
        elif args.command == "find-pending":
            found = find_pending(args.repository, run_id=args.run_id)
            print(json.dumps(found, indent=2))
            _output("pending", json.dumps(found))
        elif args.command == "preflight":
            decision = preflight(args.version, args.release_run_id, args.dry_run == "true", args.repository)
            print(f"{decision['action']}: {decision['reason']}")
            _output("action", decision["action"])
            return 1 if decision["action"] == "hold" else 0
        elif args.command == "verify-dmg":
            problems = verify_dmg(args.dmg, args.version, notarized=args.notarized == "true")
            for problem in problems:
                print(f"::error::{problem}")
            if problems:
                return 1
            print(f"{args.dmg.name}: signed by {TEAM_ID}, app {args.version}"
                  + (", both stapled" if args.notarized == "true" else ""))
        elif args.command == "attach":
            outcome = attach(args.dmg, args.version, args.release_run_id, args.repository, record=args.record)
            _output("outcome", outcome)
            print(f"attach: {outcome}")
            return 1 if outcome == "hold" else 0
    except NotarizeError as exc:
        print(f"::error::{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
