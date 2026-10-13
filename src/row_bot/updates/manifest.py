"""``row-bot-update-<version>.json``: what a release offers a background update.

The release pipeline publishes this file next to the installers. Its SHA-256 is
listed in the release body's manifest block (``updater.parse_manifest``), so the
trust chain is the same as for the installers: GitHub release → manifest block
→ this file → the SHA-256 of every part it names → the hash of every wheel in
the ``pylock.toml`` export.

Schema 1::

    {
      "schema": 1,
      "version": "5.2.1",
      "min_source_version": "5.2.0",
      "code":   {"file": "row-bot-code-5.2.1.tar.xz", "size": 8260000, "sha256": "<hex>"},
      "assets": {"file": "row-bot-assets-5.2.1.tar.xz", "size": 41500000,
                 "sha256": "<hex>", "content_sha256": "<hex>"},
      "lock":   {"hash": "sha256:<hex of uv.lock>",
                 "pylock": {"file": "row-bot-pylock-5.2.1.toml", "size": 609000, "sha256": "<hex>"}},
      "platforms": {
        "windows-x86_64": {"python": "3.13.2", "installer": "Row-Bot-5.2.1-Windows-x64.exe"},
        "macos-arm64":    {"python": "3.12.13", "installer": "Row-Bot-5.2.1-macOS-arm64.dmg"},
        "linux-x86_64":   {"python": "3.12.13", "installer": "Row-Bot-5.2.1-Linux-x86_64.tar.gz"}
      }
    }

``file`` values are asset names of the same GitHub release. Archives are
``tar.xz`` (or ``tar.gz``) whose members are relative to a version's ``app/``
folder. ``content_sha256`` is :func:`assets_content_hash` of the files the
assets archive unpacks to.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as _platform
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from row_bot import install_layout

SCHEMA = 1
#: Folders of ``app/`` that make up the assets part.
ASSET_DIRS = ("static", "sounds")
MAX_MANIFEST_BYTES = 64 * 1024
MAX_PART_BYTES = 4 * 1024**3

_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,159}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PYTHON_VERSION = re.compile(r"^\d+\.\d+\.\d+$")
_PLATFORM_KEY = re.compile(r"^(windows|macos|linux)-(x86_64|arm64|aarch64)$")


class ManifestError(ValueError):
    """The update description is missing, malformed or not for this release."""


@dataclass(frozen=True)
class Part:
    """One downloadable file of a release, checked by size and SHA-256."""

    file: str
    size: int
    sha256: str
    content_sha256: str | None = None


@dataclass(frozen=True)
class PlatformEntry:
    python: str
    installer: str | None = None


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    min_source_version: str | None
    code: Part
    assets: Part
    lock_hash: str | None
    pylock: Part | None
    platforms: Mapping[str, PlatformEntry] = field(default_factory=dict)

    def platform(self, key: str) -> PlatformEntry | None:
        return self.platforms.get(key)


def platform_key(system: str | None = None, machine: str | None = None) -> str | None:
    """``windows-x86_64``, ``macos-arm64``, ``linux-aarch64`` … for this computer."""
    name = (system or _platform.system()).lower()
    arch = (machine or _platform.machine()).lower()
    os_name = {"windows": "windows", "darwin": "macos", "linux": "linux"}.get(name)
    if os_name is None:
        return None
    if arch in {"amd64", "x86_64", "x64"}:
        return f"{os_name}-x86_64"
    if arch in {"arm64", "aarch64"}:
        return f"{os_name}-arm64" if os_name in {"macos", "windows"} else f"{os_name}-aarch64"
    return None


def _part(value: object, label: str, *, content: bool = False) -> Part:
    if not isinstance(value, dict):
        raise ManifestError(f"{label} is missing")
    name, size, digest = value.get("file"), value.get("size"), value.get("sha256")
    if not isinstance(name, str) or not _FILE_NAME.fullmatch(name):
        raise ManifestError(f"{label}.file is not a release file name")
    if type(size) is not int or not 0 < size <= MAX_PART_BYTES:
        raise ManifestError(f"{label}.size is not a size")
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest.lower()):
        raise ManifestError(f"{label}.sha256 is not a SHA-256")
    content_hash = None
    if content:
        content_hash = value.get("content_sha256")
        if not isinstance(content_hash, str) or not _SHA256.fullmatch(content_hash.lower()):
            raise ManifestError(f"{label}.content_sha256 is not a SHA-256")
        content_hash = content_hash.lower()
    return Part(name, size, digest.lower(), content_hash)


def parse_manifest(raw: bytes | str, *, expected_version: str | None = None) -> UpdateManifest:
    """Validate an update description. Raises :class:`ManifestError`."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise ManifestError("update description is too large")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError("update description is not JSON") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ManifestError("unsupported update description schema")
    version = data.get("version")
    if not install_layout.is_valid_version_name(version):
        raise ManifestError("version is not valid")
    if expected_version is not None and version != expected_version:
        raise ManifestError(f"describes {version}, not {expected_version}")
    minimum = data.get("min_source_version")
    if minimum is not None and not install_layout.is_valid_version_name(minimum):
        raise ManifestError("min_source_version is not valid")
    lock = data.get("lock")
    lock_hash: str | None = None
    pylock: Part | None = None
    if lock is not None:
        if not isinstance(lock, dict):
            raise ManifestError("lock is not an object")
        raw_hash = lock.get("hash")
        if raw_hash is not None:
            if not isinstance(raw_hash, str) or not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", raw_hash):
                raise ManifestError("lock.hash is not sha256:<hex>")
            lock_hash = raw_hash.lower()
        if lock.get("pylock") is not None:
            pylock = _part(lock.get("pylock"), "lock.pylock")
    platforms: dict[str, PlatformEntry] = {}
    raw_platforms = data.get("platforms") or {}
    if not isinstance(raw_platforms, dict):
        raise ManifestError("platforms is not an object")
    for key, entry in raw_platforms.items():
        if not isinstance(key, str) or not _PLATFORM_KEY.fullmatch(key) or not isinstance(entry, dict):
            raise ManifestError("platforms has an unknown entry")
        python = entry.get("python")
        if not isinstance(python, str) or not _PYTHON_VERSION.fullmatch(python):
            raise ManifestError(f"platforms.{key}.python is not a Python version")
        installer = entry.get("installer")
        if installer is not None and (not isinstance(installer, str) or not _FILE_NAME.fullmatch(installer)):
            raise ManifestError(f"platforms.{key}.installer is not a release file name")
        platforms[key] = PlatformEntry(python, installer)
    return UpdateManifest(
        version=str(version),
        min_source_version=minimum,
        code=_part(data.get("code"), "code"),
        assets=_part(data.get("assets"), "assets", content=True),
        lock_hash=lock_hash,
        pylock=pylock,
        platforms=platforms,
    )


def manifest_file_name(version: str) -> str:
    return f"row-bot-update-{version}.json"


def assets_content_hash(app_root: Path, subdirs: tuple[str, ...] = ASSET_DIRS) -> str:
    """SHA-256 over the asset files under ``app_root``, independent of archives.

    For every regular file under ``app_root/<subdir>`` in ascending order of its
    path relative to ``app_root`` (``/``-separated): the UTF-8 relative path, a
    NUL byte, then the 32-byte SHA-256 of the file's bytes.
    """
    files: list[tuple[str, Path]] = []
    for sub in subdirs:
        base = Path(app_root) / sub
        if not base.is_dir():
            continue
        for folder, _dirs, names in os.walk(base):
            for name in names:
                path = Path(folder) / name
                if path.is_symlink() or not path.is_file():
                    continue
                files.append((path.relative_to(app_root).as_posix(), path))
    outer = hashlib.sha256()
    for relative, path in sorted(files):
        inner = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                inner.update(chunk)
        outer.update(relative.encode("utf-8") + b"\0")
        outer.update(inner.digest())
    return outer.hexdigest()


def read_version_metadata(version_root: Path) -> dict[str, Any]:
    """A version folder's ``version.json``, or ``{}``."""
    try:
        data = json.loads((Path(version_root) / install_layout.VERSION_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


__all__ = [
    "ASSET_DIRS",
    "ManifestError",
    "Part",
    "PlatformEntry",
    "UpdateManifest",
    "assets_content_hash",
    "manifest_file_name",
    "parse_manifest",
    "platform_key",
    "read_version_metadata",
]
