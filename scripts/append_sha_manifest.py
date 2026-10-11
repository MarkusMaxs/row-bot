"""Write the SHA-256 manifest block of a GitHub release body.

The block is what Row-Bot's updater trusts: every release file it downloads
(the installers 5.1 runs, and the update description and parts a background
update uses) must be listed with its SHA-256::

    <!-- row-bot-update-manifest -->
    ```manifest
    schema: 1
    files:
      Row-Bot-5.2.0-Windows-x64.exe: sha256=<hex>
      row-bot-update-5.2.0.json: sha256=<hex>
    ```

Modes:

``--listing L --body-file IN --write-body OUT``
    Offline: replace the block in a body file with exactly the files of a
    listing (``[{"name", "size", "sha256"}]``). ``scripts/release_windows.ps1``
    writes the draft's block this way from the final files, before publishing.
``--tag vX --files F...``
    Replace the block of a published release with exactly these files.
``--tag vX --files F... --add``
    Add these files to the block, or update their lines; keep every other line
    (``container.yml`` adds the container digests after publishing).
``--tag vX --files F... --backstop``
    ``update-manifest.yml``: write the block when there is none and add files
    it does not list, but never change a listed hash. A listed file whose
    bytes differ fails instead, so a correct block is never overwritten.

Every mode rewrites only the block and leaves the rest of the body alone, and
running it twice gives the same body.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import re
import sys
import urllib.request

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _path in (_REPO_ROOT, _REPO_ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from row_bot.brand import APP_REPOSITORY, UPDATE_MANIFEST_MARKER, UPDATER_USER_AGENT  # noqa: E402

_MANIFEST_RE = re.compile(
    rf"<!--\s*{re.escape(UPDATE_MANIFEST_MARKER)}\s*-->\s*```manifest(.*?)```",
    re.DOTALL | re.IGNORECASE,
)
_LINE_RE = re.compile(r"^\s*([A-Za-z0-9._+-]+):\s*sha256\s*=\s*([0-9a-fA-F]{64})\s*$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,159}$")


class ManifestConflict(Exception):
    """A listed file's hash differs from the file now in the release."""


def sha256_of(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for chunk in iter(lambda: fp.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def build_manifest_block(file_hashes: dict[str, str]) -> str:
    lines = [f"<!-- {UPDATE_MANIFEST_MARKER} -->", "```manifest", "schema: 1", "files:"]
    for name in sorted(file_hashes):
        if not _NAME_RE.fullmatch(name) or not re.fullmatch(r"[0-9a-f]{64}", file_hashes[name]):
            raise ValueError(f"not a release file and SHA-256: {name}")
        lines.append(f"  {name}: sha256={file_hashes[name]}")
    lines.append("```")
    return "\n".join(lines)


def parse_block(body: str) -> dict[str, str] | None:
    """The files the body's block lists, or None when it has no block."""
    match = _MANIFEST_RE.search(body or "")
    if not match:
        return None
    out: dict[str, str] = {}
    for line in match.group(1).splitlines():
        found = _LINE_RE.match(line)
        if found:
            out[found.group(1)] = found.group(2).lower()
    return out


def merge_into_body(body: str, manifest_block: str) -> str:
    if _MANIFEST_RE.search(body or ""):
        return _MANIFEST_RE.sub(lambda _match: manifest_block, body, count=1)
    if not body:
        return manifest_block + "\n"
    return body.rstrip() + "\n\n" + manifest_block + "\n"


def merged_hashes(existing: dict[str, str] | None, files: dict[str, str], mode: str) -> dict[str, str]:
    """The block's files after *mode* (``replace``, ``add`` or ``backstop``)."""
    if mode == "replace" or existing is None:
        return dict(files)
    if mode == "add":
        return {**existing, **files}
    conflicts = sorted(name for name, digest in files.items() if name in existing and existing[name] != digest)
    if conflicts:
        raise ManifestConflict(
            "the release's files no longer match its manifest block: " + ", ".join(conflicts)
            + ". The block is left as it is; check the release and rewrite it on purpose."
        )
    return {**existing, **{name: digest for name, digest in files.items() if name not in existing}}


def _api(url: str, token: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": UPDATER_USER_AGENT,
            "X-GitHub-Api-Version": "2022-11-28",
            **({"Content-Type": "application/json"} if data else {}),
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec - api.github.com only
        return json.loads(resp.read().decode("utf-8"))


def _hash_files(raw_files: list[str]) -> dict[str, str]:
    file_hashes: dict[str, str] = {}
    for raw in raw_files:
        # Allow comma-separated list in a single arg (handy for some shells)
        for part in str(raw).replace(",", " ").split():
            p = pathlib.Path(part).expanduser().resolve()
            if not p.exists():
                print(f"warning: skipping missing file {p}", file=sys.stderr)
                continue
            file_hashes[p.name] = sha256_of(p)
            print(f"{p.name}: sha256={file_hashes[p.name]}")
    return file_hashes


def _offline(args: argparse.Namespace) -> int:
    listing = json.loads(pathlib.Path(args.listing).read_text(encoding="utf-8"))
    files = {str(item["name"]): str(item["sha256"]).lower() for item in listing}
    if not files:
        print("error: the listing is empty", file=sys.stderr)
        return 2
    body = pathlib.Path(args.body_file).read_text(encoding="utf-8") if args.body_file else ""
    new_body = merge_into_body(body, build_manifest_block(files))
    if parse_block(new_body) != files:
        print("error: the written block does not read back as the listing", file=sys.stderr)
        return 1
    pathlib.Path(args.write_body).write_text(new_body, encoding="utf-8", newline="\n")
    print(f"Manifest block lists {len(files)} files")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--tag")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY") or APP_REPOSITORY)
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN"))
    parser.add_argument("--files", nargs="+", help="Artifact paths to include in the manifest")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--add", action="store_true", help="add or update these files, keep the other lines")
    mode.add_argument("--backstop", action="store_true", help="fill a missing block, never change a listed hash")
    parser.add_argument("--listing", help="offline: JSON list of {name, size, sha256}")
    parser.add_argument("--body-file", help="offline: the current release body")
    parser.add_argument("--write-body", help="offline: where to write the new body")
    args = parser.parse_args(argv)

    if args.listing:
        if not args.write_body:
            parser.error("--listing needs --write-body")
        return _offline(args)
    if not args.tag or not args.files:
        parser.error("--tag and --files are required (or --listing for the offline mode)")
    if not args.repo:
        print("error: --repo or GITHUB_REPOSITORY required", file=sys.stderr)
        return 2
    if not args.token:
        print("error: --token or GITHUB_TOKEN required", file=sys.stderr)
        return 2

    file_hashes = _hash_files(args.files)
    if not file_hashes:
        print("error: no files to hash", file=sys.stderr)
        return 2

    # Fetch release (published releases only: a draft's block is written offline)
    tag = args.tag.lstrip("v")
    api_base = f"https://api.github.com/repos/{args.repo}"
    release = _api(f"{api_base}/releases/tags/v{tag}", args.token)
    body = release.get("body") or ""
    try:
        files = merged_hashes(parse_block(body), file_hashes,
                              "add" if args.add else "backstop" if args.backstop else "replace")
    except ManifestConflict as exc:
        print(f"::error::{exc}")
        return 1
    new_body = merge_into_body(body, build_manifest_block(files))
    if new_body == body:
        print("manifest unchanged — skipping PATCH")
        return 0

    _api(
        f"{api_base}/releases/{release['id']}",
        args.token,
        method="PATCH",
        payload={"body": new_body},
    )
    print(f"Manifest block of v{tag} lists {len(files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
