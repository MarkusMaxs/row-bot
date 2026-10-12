"""Asynchronous macOS notarization for release.yml and notarize-finish.yml (D3).

The release submits the signed app as soon as it is built and does other work
while Apple checks it. It then waits, for a bounded time, staples the app,
builds and signs the DMG from it, and notarizes and staples the DMG the same
way. When a wait runs out, the job uploads what it has and the submission ids
as a ``macos-notarization-pending-<version>`` artifact; ``notarize-finish.yml``
picks that up later. A release's DMG is attached to its draft only once it is
stapled, so a DMG on the draft means macOS is ready.

State (``notarization.json``, next to the files it describes)::

    {"schema": 1, "version": "5.2.0", "dry_run": false, "release_run_id": "123",
     "app": {"file": "Row-Bot.app.zip", "submission_id": "...", "status": "Accepted",
             "submitted_at": "...", "stapled": true},
     "dmg": {"file": "Row-Bot-5.2.0-macOS-arm64.dmg", "submission_id": "...", ...}}

Apple credentials come from APPLE_ID, APPLE_APP_PASSWORD and APPLE_TEAM_ID and
are never printed. Subcommands: ``submit``, ``wait``, ``staple``, ``skip``,
``summary`` and ``find-pending``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

SCHEMA = 1
POLL_SECONDS = 30
SUBMIT_ATTEMPTS = 3
STAPLE_ATTEMPTS = 5
PENDING_ARTIFACT = re.compile(r"^macos-notarization-pending-(?P<version>[0-9A-Za-z][0-9A-Za-z.+-]*?)(?P<dry>-dry-run)?$")
#: Workflows whose runs may hold a pending notarization.
WORKFLOWS = ("release.yml", "notarize-finish.yml")
#: notarize-finish looks back this far for pending notarizations.
LOOK_BACK_DAYS = 7

Runner = Callable[..., subprocess.CompletedProcess]


class NotarizeError(Exception):
    pass


def pending_artifact_name(version: str, dry_run: bool) -> str:
    return f"macos-notarization-pending-{version}" + ("-dry-run" if dry_run else "")


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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


def _notarytool(run: Runner, *args: str) -> tuple[int, dict[str, Any], str]:
    completed = run(["xcrun", "notarytool", *args, *_credentials(), "--output-format", "json"],
                    capture_output=True, text=True, check=False)
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
        code, payload, error = _notarytool(run, "submit", str(file), "--no-wait")
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
    deadline = clock() + minutes * 60
    status = str(entry.get("status") or "In Progress")
    while True:
        code, payload, error = _notarytool(run, "info", submission)
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
    log = run(["xcrun", "notarytool", "log", submission, *_credentials()], capture_output=True, text=True, check=False)
    print(log.stdout)
    return "invalid"


def staple(state_path: Path, label: str, target: Path, *, run: Runner = subprocess.run,
           sleep: Callable[[float], None] = time.sleep) -> None:
    """Staple the ticket (Apple's servers can take minutes to serve it), then validate."""
    for attempt in range(1, STAPLE_ATTEMPTS + 1):
        if run(["xcrun", "stapler", "staple", str(target)], check=False).returncode == 0:
            break
        if attempt == STAPLE_ATTEMPTS:
            raise NotarizeError(f"{label}: the ticket could not be stapled to {target.name}")
        sleep(30 * attempt)
    if run(["xcrun", "stapler", "validate", str(target)], check=False).returncode != 0:
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
) -> str:
    """Everything after the app's submission, resumable at any point.

    Waits for the app and staples it, builds and signs the DMG from it,
    notarizes and staples the DMG. Returns ``ready``, ``app-only`` (dry run:
    the DMG is not submitted), ``pending`` or ``failed``. On ``pending`` the
    state folder holds what a later run needs: the app's zip while the app is
    being checked, the DMG while the DMG is.
    """
    folder = state_path.parent
    state = load_state(state_path)
    app_entry = state.get("app") or {}
    if not app_entry.get("stapled"):
        status = wait(state_path, "app", minutes, run=run, sleep=sleep, clock=clock)
        if status != "accepted":
            return "pending" if status == "pending" else "failed"
        if not app.exists():
            zipped = folder / str(app_entry.get("file") or "Row-Bot.app.zip")
            app.parent.mkdir(parents=True, exist_ok=True)
            if run(["ditto", "-x", "-k", str(zipped), str(app.parent)], check=False).returncode != 0:
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
        status = wait(state_path, "dmg", minutes, run=run, sleep=sleep, clock=clock)
        if status == "pending":
            if dmg.parent.resolve() != folder.resolve():
                os.replace(dmg, folder / dmg.name)
            return "pending"
        if status != "accepted":
            return "failed"
        staple(state_path, "dmg", dmg, run=run, sleep=sleep)
    return "ready"


def readiness(state: dict[str, Any]) -> str:
    """``ready`` (DMG stapled), ``app-only`` (dry run), ``pending`` or ``failed``."""
    dmg, app = state.get("dmg") or {}, state.get("app") or {}
    if dmg.get("stapled"):
        return "ready"
    if dmg.get("skipped") and app.get("stapled"):
        return "app-only"
    if "Invalid" in (dmg.get("status"), app.get("status")) or "Rejected" in (dmg.get("status"), app.get("status")):
        return "failed"
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
        lines.append("macOS is ready: the stapled DMG is attached to the draft release (or, in a dry run, to this run).")
    elif status == "app-only":
        lines.append("Dry run: the app is notarized and stapled; the DMG was signed but not submitted.")
    elif status == "pending":
        lines.append(f"Apple is still checking. `{pending_artifact_name(version, bool(state.get('dry_run')))}` "
                     "holds the files; `notarize-finish.yml` finishes them (hourly, or run it now with this run's id). "
                     "The release cannot be published until the stapled DMG is on the draft.")
    else:
        lines.append("Apple refused the submission: see its log above. Fix the build and run the release again.")
    return "\n".join(lines) + "\n"


def find_pending(repository: str, *, run_id: str | None = None, include_dry_runs: bool = False,
                 gh: Callable[[list[str]], Any] | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Pending notarizations: their runs' ``macos-notarization-pending-*`` artifacts."""
    query = gh or _gh_json
    runs: Iterable[dict[str, Any]]
    if run_id:
        runs = [{"id": int(run_id), "conclusion": "success", "created_at": None}]
        include_dry_runs = True
    else:
        runs = []
        for workflow in WORKFLOWS:
            listed = query(["api", f"repos/{repository}/actions/workflows/{workflow}/runs?status=completed&per_page=50"])
            runs = [*runs, *((listed or {}).get("workflow_runs") or [])]
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=LOOK_BACK_DAYS)
    found: list[dict[str, Any]] = []
    for item in runs:
        if item.get("conclusion") != "success":
            continue
        created = item.get("created_at")
        if created and datetime.fromisoformat(str(created).replace("Z", "+00:00")) < cutoff:
            continue
        artifacts = query(["api", f"repos/{repository}/actions/runs/{item['id']}/artifacts?per_page=100"])
        for artifact in (artifacts or {}).get("artifacts") or []:
            match = PENDING_ARTIFACT.fullmatch(str(artifact.get("name") or ""))
            if not match or artifact.get("expired"):
                continue
            dry_run = bool(match.group("dry"))
            if dry_run and not include_dry_runs:
                continue
            found.append({"run_id": str(item["id"]), "artifact_id": str(artifact["id"]),
                          "artifact": artifact["name"], "version": match.group("version"), "dry_run": dry_run})
    return found


def _gh_json(args: list[str]) -> Any:
    completed = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise NotarizeError(f"gh {' '.join(args[:2])} failed: {completed.stderr.strip()}")
    return json.loads(completed.stdout or "null")


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
    rest.add_argument("--identity", default=os.environ.get("CODESIGN_IDENTITY", ""))
    rest.add_argument("--dry-run", action="store_true")
    report = commands.add_parser("summary")
    report.add_argument("--state", type=Path, required=True)
    scan = commands.add_parser("find-pending")
    scan.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    scan.add_argument("--run-id")
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
                if subprocess.run(["bash", str(script), str(app), str(dmg), args.identity], check=False).returncode:
                    raise NotarizeError(f"building {dmg.name} failed")

            result = finish(args.state, app=args.app, dmg=args.dmg, minutes=args.minutes,
                            dry_run=args.dry_run, build_dmg=build_dmg)
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
    except NotarizeError as exc:
        print(f"::error::{exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
