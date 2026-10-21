"""The Windows installer stage: packed in CI, unpacked and checked on the signing machine.

``Row-Bot.exe`` must be signed before Inno Setup packs it, and the Certum
certificate never leaves the maintainer's machine (D4). release.yml therefore
ships the stage ISCC needs, laid out as in a checkout:

- ``installer/row_bot_setup.iss`` and ``row-bot.ico``
- ``installer/build/{Row-Bot.exe,Row-Bot Server.exe,version.json,inno-version.txt}``
- ``installer/build/app/`` and ``installer/build/python/`` (about 86,000 files)

``pack`` writes them into one tar (Python's tarfile: Windows' bsdtar can skip
files whose NTFS file id matches the archive's) together with
``installer/build/stage.json``, which names the release run and commit that
built the stage and counts every file and byte. ``unpack`` extracts it into an
empty folder, refuses a stage from another run or commit, checks the count and
only then writes ``installer/build/stage.verified.json``. ``check`` decides
whether an unpacked stage may be used again: the verified marker must name the
same run and commit, and the files must still count up. Anything else is
unpacked afresh, so a stage is never signed for a run that did not build it.

    python scripts/windows_stage.py pack --root . --out stage.tar          # run id and commit from GITHUB_RUN_ID, GITHUB_SHA
    python scripts/windows_stage.py unpack --archive stage.tar --out work/s --run-id 123 --sha <commit>
    python scripts/windows_stage.py check --stage work/s --run-id 123 --sha <commit>
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import sys
import tarfile
from pathlib import Path
from typing import Any

FILES = ("installer/row_bot_setup.iss", "row-bot.ico", "installer/build/Row-Bot.exe",
         "installer/build/Row-Bot Server.exe", "installer/build/version.json", "installer/build/inno-version.txt")
FOLDERS = ("installer/build/app", "installer/build/python")
STAGE_JSON = "installer/build/stage.json"
VERIFIED_JSON = "installer/build/stage.verified.json"
#: Signed in place on the signing machine, so their bytes are not counted.
SIGNED_IN_PLACE = ("installer/build/Row-Bot.exe", "installer/build/Row-Bot Server.exe")


class StageError(Exception):
    pass


def _walk(root: Path) -> list[str]:
    names: list[str] = []
    for relative in FILES:
        if not (root / relative).is_file():
            raise StageError(f"{relative} is missing")
        names.append(relative)
    for relative in FOLDERS:
        if not (root / relative).is_dir():
            raise StageError(f"{relative} is missing")
        for folder, dirs, files in os.walk(root / relative):
            dirs.sort()
            for name in sorted(files):
                names.append((Path(folder) / name).relative_to(root).as_posix())
    return names


def count(root: Path) -> dict[str, int]:
    """Files and bytes of a stage (the two programs counted, their bytes not: they get signed)."""
    names = _walk(root)
    total = sum((root / name).stat().st_size for name in names if name not in SIGNED_IN_PLACE)
    return {"files": len(names), "bytes": total}


def _identity(run_id: str, sha: str) -> dict[str, str]:
    if not re.fullmatch(r"\d+", str(run_id or "")) or not re.fullmatch(r"[0-9a-f]{40}", str(sha or "")):
        raise StageError("a stage needs the release run id and the full commit sha")
    return {"run_id": str(run_id), "sha": str(sha)}


def pack(root: Path, out: Path, *, run_id: str, sha: str) -> dict[str, Any]:
    summary = {**_identity(run_id, sha), **count(root)}
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as tar:
        for name in _walk(root):
            tar.add(root / name, arcname=name, recursive=False)
        # Empty folders count for Inno Setup's createallsubdirs.
        for relative in FOLDERS:
            for folder, dirs, files in os.walk(root / relative):
                if not dirs and not files:
                    tar.add(folder, arcname=Path(folder).relative_to(root).as_posix(), recursive=False)
        data = json.dumps(summary).encode()
        info = tarfile.TarInfo(STAGE_JSON)
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return summary


def unpack(archive: Path, out: Path, *, run_id: str, sha: str) -> dict[str, Any]:
    """Extract into an empty folder; the verified marker is written only after every check."""
    expected = _identity(run_id, sha)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        tar.extractall(out, filter="data")
    try:
        packed = json.loads((out / STAGE_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StageError(f"the stage has no readable {STAGE_JSON}") from exc
    if {key: str(packed.get(key)) for key in expected} != expected:
        raise StageError(f"the stage was built by run {packed.get('run_id')} at {packed.get('sha')}, "
                         f"not by run {run_id} at {sha}")
    found = count(out)
    if found != {"files": packed.get("files"), "bytes": packed.get("bytes")}:
        raise StageError(f"the unpacked stage has {found}, the packed one had {packed}")
    marker = {**expected, **found}
    (out / VERIFIED_JSON).write_text(json.dumps(marker), encoding="utf-8", newline="\n")
    return marker


def check(stage: Path, *, run_id: str, sha: str) -> str | None:
    """Why an unpacked stage cannot be used for this run (None: it can)."""
    expected = _identity(run_id, sha)
    try:
        marker = json.loads((stage / VERIFIED_JSON).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "it was never unpacked and checked completely"
    if {key: str(marker.get(key)) for key in expected} != expected:
        return f"it belongs to run {marker.get('run_id')} at {marker.get('sha')}"
    try:
        found = count(stage)
    except StageError as exc:
        return str(exc)
    if found != {"files": marker.get("files"), "bytes": marker.get("bytes")}:
        return f"its files changed since it was checked ({found} instead of {marker})"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("pack")
    make.add_argument("--root", type=Path, default=Path("."))
    make.add_argument("--out", type=Path, required=True)
    make.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    make.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""))
    for name in ("unpack", "check"):
        sub = commands.add_parser(name)
        if name == "unpack":
            sub.add_argument("--archive", type=Path, required=True)
            sub.add_argument("--out", type=Path, required=True)
        else:
            sub.add_argument("--stage", type=Path, required=True)
        sub.add_argument("--run-id", required=True)
        sub.add_argument("--sha", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "pack":
            summary = pack(args.root, args.out, run_id=args.run_id, sha=args.sha)
        elif args.command == "unpack":
            summary = unpack(args.archive, args.out, run_id=args.run_id, sha=args.sha)
        else:
            reason = check(args.stage, run_id=args.run_id, sha=args.sha)
            if reason:
                print(f"The stage in {args.stage} cannot be used: {reason}")
                return 1
            print(f"The stage in {args.stage} is run {args.run_id}'s, checked")
            return 0
    except StageError as exc:
        print(f"::error::{exc}")
        return 1
    print(f"{summary['files']:,} files, {summary['bytes']:,} bytes (run {summary['run_id']}, {summary['sha'][:12]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
