"""The Windows installer stage: packed in CI, unpacked and checked on the signing machine.

``Row-Bot.exe`` must be signed before Inno Setup packs it, and the Certum
certificate never leaves the maintainer's machine (D4). release.yml therefore
ships the stage ISCC needs, laid out as in a checkout:

- ``installer/row_bot_setup.iss`` and ``row-bot.ico``
- ``installer/build/{Row-Bot.exe,version.json,inno-version.txt}``
- ``installer/build/app/`` and ``installer/build/python/`` (about 86,000 files)

``pack`` writes them into one tar (Python's tarfile: Windows' bsdtar can skip
files whose NTFS file id matches the archive's) together with
``installer/build/stage.json``, which counts every file and byte. ``unpack``
extracts it and checks the count, so a short or damaged stage is never packed
into an installer.

    python scripts/windows_stage.py pack --root . --out stage.tar
    python scripts/windows_stage.py unpack --archive stage.tar --out work/stage
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tarfile
from pathlib import Path

FILES = ("installer/row_bot_setup.iss", "row-bot.ico", "installer/build/Row-Bot.exe",
         "installer/build/version.json", "installer/build/inno-version.txt")
FOLDERS = ("installer/build/app", "installer/build/python")
STAGE_JSON = "installer/build/stage.json"


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


def pack(root: Path, out: Path) -> dict[str, int]:
    names = _walk(root)
    total = sum((root / name).stat().st_size for name in names)
    summary = {"files": len(names), "bytes": total}
    out.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(out, "w", format=tarfile.PAX_FORMAT) as tar:
        for name in names:
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


def unpack(archive: Path, out: Path) -> dict[str, int]:
    out.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as tar:
        tar.extractall(out, filter="data")
    summary = json.loads((out / STAGE_JSON).read_text(encoding="utf-8"))
    names = _walk(out)
    found = {"files": len(names), "bytes": sum((out / name).stat().st_size for name in names)}
    if found != summary:
        raise StageError(f"the unpacked stage has {found}, the packed one had {summary}")
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("pack")
    make.add_argument("--root", type=Path, default=Path("."))
    make.add_argument("--out", type=Path, required=True)
    take = commands.add_parser("unpack")
    take.add_argument("--archive", type=Path, required=True)
    take.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "pack":
            summary = pack(args.root, args.out)
        else:
            summary = unpack(args.archive, args.out)
    except StageError as exc:
        print(f"::error::{exc}")
        return 1
    print(f"{summary['files']:,} files, {summary['bytes']:,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
