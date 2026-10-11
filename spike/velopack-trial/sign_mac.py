"""Sign a trial Row-Bot.app the way installer/build_mac_app.sh does (Phase 0 spike).

Same codesign flags as the release build (--force --options runtime
--timestamp --entitlements), applied to every .so/.dylib and then every
other Mach-O executable. The bundle itself is signed later by
`vpk pack --signDisableDeep`, which also adds and signs UpdateMac.

With --cache DIR, a file whose pre-signing bytes match a file signed before
(same identity and entitlements) gets the previously signed bytes instead of
a fresh signature: the "reuse unchanged signed files" cache from PLAN.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


def is_macho(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            magic = handle.read(4)
    except OSError:
        return False
    return magic in {
        b"\xcf\xfa\xed\xfe",
        b"\xce\xfa\xed\xfe",
        b"\xfe\xed\xfa\xcf",
        b"\xfe\xed\xfa\xce",
        b"\xca\xfe\xba\xbe",
        b"\xbe\xba\xfe\xca",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--entitlements", type=Path, required=True)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--read-cache", action="store_true", help="reuse signed files from --cache")
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()

    app = args.app.resolve()
    main_exe = app / "Contents" / "MacOS"
    libs = sorted(
        p for p in app.rglob("*") if p.is_file() and not p.is_symlink() and p.name.endswith((".so", ".dylib"))
    )
    lib_set = set(libs)
    exes = sorted(
        p
        for p in app.rglob("*")
        if p.is_file()
        and not p.is_symlink()
        and p not in lib_set
        and os.access(p, os.X_OK)
        and is_macho(p)
        and p.parent != main_exe  # the bundle signature covers Contents/MacOS
    )
    salt = hashlib.sha256(
        (args.identity + "\0").encode() + args.entitlements.read_bytes()
    ).hexdigest()[:16]
    if args.cache:
        args.cache.mkdir(parents=True, exist_ok=True)

    stats = {"signed": 0, "reused": 0, "failed": 0, "seconds": 0.0}
    failures: list[str] = []

    def key_for(path: Path) -> str:
        return hashlib.sha256(salt.encode() + path.read_bytes()).hexdigest()

    def sign(path: Path) -> str:
        key = key_for(path) if args.cache else None
        cached = args.cache / key if key else None
        if args.read_cache and cached is not None and cached.exists():
            mode = path.stat().st_mode
            shutil.copyfile(cached, path)
            os.chmod(path, mode)
            return "reused"
        proc = subprocess.run(  # noqa: S603 - CI signing
            [
                "codesign",
                "--force",
                "--options",
                "runtime",
                "--timestamp",
                "--entitlements",
                str(args.entitlements),
                "--sign",
                args.identity,
                str(path),
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            failures.append(f"{path}: {proc.stderr.strip()[:300]}")
            return "failed"
        if cached is not None:
            shutil.copyfile(path, cached)
        return "signed"

    started = time.time()
    # Libraries first, then executables (inside-out, like build_mac_app.sh).
    for group in (libs, exes):
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            for outcome in pool.map(sign, group):
                stats[outcome] += 1
    stats["seconds"] = round(time.time() - started, 1)
    report = {
        "app": str(app),
        "libraries": len(libs),
        "executables": [str(p.relative_to(app)) for p in exes],
        "cache": str(args.cache) if args.cache else None,
        "read_cache": args.read_cache,
        **stats,
        "failures": failures[:50],
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "executables"}, indent=2))
    if failures:
        raise SystemExit(f"{len(failures)} codesign failures")


if __name__ == "__main__":
    main()
