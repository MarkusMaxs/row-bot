"""Helpers for the 5.1 -> 5.2 bridge checks in installer-verify.yml.

Row-Bot 5.1 updates by downloading the full installer of a newer release:
it runs the Windows installer silently, opens the macOS DMG, and installs the
Linux tarball itself. installer-verify proves the new packages work that way,
starting from the real 5.1.0 release.

``asset-names``
    Runs 5.1's actual ``updater._parse_release`` (from ``git archive v5.1.0
    src``) against every file a new release publishes and checks it picks this
    platform's installer. The legacy source runs in its own process (it is also
    called ``row_bot``) and needs ``packaging``.
``download``
    Downloads one file of a published release with ``gh`` and checks it against
    that release's SHA-256 manifest block, as 5.1 would.

    python scripts/bridge_51.py asset-names --version 5.2.0 --legacy-src /tmp/v5.1.0/src
    python scripts/bridge_51.py download --tag v5.1.0 --name Row-Bot-5.1.0-Linux-x86_64.tar.gz --out /tmp/v5.1.0
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import append_sha_manifest  # noqa: E402
import build_update_parts as parts  # noqa: E402
from row_bot.updates.manifest import platform_key  # noqa: E402

_PICK = """
import json, sys
from row_bot import updater
names = json.loads(sys.argv[1])
release = {"tag_name": "v" + sys.argv[2], "body": "", "prerelease": False, "published_at": "",
           "assets": [{"name": name, "size": 1, "browser_download_url": "https://github.com/o/r/releases/download/x/" + name}
                      for name in names]}
info = updater._parse_release(release, "stable")
print(json.dumps({"asset": info.asset_name if info else None, "version": info.version if info else None}))
"""


def asset_names(version: str, legacy_src: Path) -> int:
    expected = parts.installer_name(platform_key(), version)
    names = [*parts.release_file_names(version), parts.container_name(version)]
    # 5.1 takes the first match in upload order: list the installers last, so
    # this also fails if 5.1 would pick one of the new files.
    names = sorted(names, key=lambda name: name.startswith("Row-Bot-"))
    env = {**os.environ, "PYTHONPATH": str(Path(legacy_src).resolve()),
           "ROW_BOT_DATA_DIR": str(Path(os.environ.get("RUNNER_TEMP", ".")) / "bridge-legacy-data")}
    picked = subprocess.run([sys.executable, "-c", _PICK, json.dumps(names), version],
                            capture_output=True, text=True, env=env, check=False)
    if picked.returncode != 0:
        print(f"::error::5.1's updater did not run: {picked.stderr.strip()[-2000:]}")
        return 1
    result = json.loads(picked.stdout.strip().splitlines()[-1])
    if result != {"asset": expected, "version": version}:
        print(f"::error::On {platform.system()} 5.1's updater picks {result}, not {expected}")
        return 1
    print(f"5.1's updater on {platform_key()} picks {expected} from the {len(names)} files of {version}")
    return 0


def download(tag: str, name: str, out: Path, repository: str) -> int:
    out.mkdir(parents=True, exist_ok=True)
    repo = ["-R", repository] if repository else []
    subprocess.run(["gh", "release", "download", tag, *repo, "-p", name, "-D", str(out), "--clobber"], check=True)
    body = subprocess.run(["gh", "release", "view", tag, *repo, "--json", "body", "--jq", ".body"],
                          check=True, capture_output=True, text=True).stdout
    listed = append_sha_manifest.parse_block(body) or {}
    digest = append_sha_manifest.sha256_of(out / name)
    if listed.get(name) != digest:
        print(f"::error::{name} does not match the {tag} manifest block")
        return 1
    print(f"{name}: matches the {tag} manifest block ({digest})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    commands = parser.add_subparsers(dest="command", required=True)
    names = commands.add_parser("asset-names")
    names.add_argument("--version", required=True)
    names.add_argument("--legacy-src", type=Path, required=True)
    fetch = commands.add_parser("download")
    fetch.add_argument("--tag", required=True)
    fetch.add_argument("--name", required=True)
    fetch.add_argument("--out", type=Path, required=True)
    fetch.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    args = parser.parse_args(argv)
    if args.command == "asset-names":
        return asset_names(args.version, args.legacy_src)
    return download(args.tag, args.name, args.out, args.repository)


if __name__ == "__main__":
    raise SystemExit(main())
