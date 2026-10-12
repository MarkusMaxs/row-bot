"""Decide what a background update downloads and how it builds the runtime.

- **Code** is downloaded every time.
- **Assets** only when their content hash differs from the running version's.
- **Runtime** (``python/``):

  - ``reuse``: same Python and the lock is unchanged (or no library differs):
    the new version gets a clone of the running ``python/``.
  - ``sync``: same Python, the lock changed and every changed or added library
    has a wheel for this computer: clone without the changed libraries, then
    install exactly those wheels from local files (pip, ``--require-hashes``).
  - ``full``: the Python version changed, the release needs a newer source
    version, or a changed library has no wheel. The engine then offers the
    full installer instead (D15, D16).

The planner never asks a package index: wheel URLs and hashes come from the
release's ``pylock.toml`` export of ``uv.lock``.
"""

from __future__ import annotations

import importlib.metadata
import re
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping

from row_bot import install_layout
from row_bot.updates.manifest import Part, UpdateManifest

#: Installed outside the lock by every build; never removed by a sync.
KEEP_DISTS = frozenset({"pip", "setuptools", "wheel"})
#: A wheel's file name is used as a path in the download folder: one plain
#: name, no separators (packaging alone accepts a build tag like "1/../..").
_WHEEL_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,200}\.whl$")


def safe_wheel_name(filename: str) -> bool:
    """A wheel's file name from a lock is used as a path: a plain basename only."""
    return bool(_WHEEL_FILE_NAME.fullmatch(filename)) and Path(filename).name == filename


RuntimeMode = Literal["reuse", "sync", "full"]


def canonical_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass(frozen=True)
class InstalledDist:
    name: str
    version: str
    dist_info: str
    #: Files the distribution owns, as paths relative to site-packages.
    files: tuple[str, ...] = ()


@dataclass(frozen=True)
class WheelPick:
    name: str
    version_from: str | None
    version_to: str
    url: str
    filename: str
    sha256: str
    size: int | None


@dataclass
class RuntimePlan:
    mode: RuntimeMode
    reason: str
    change: list[WheelPick] = field(default_factory=list)
    add: list[WheelPick] = field(default_factory=list)
    remove: list[InstalledDist] = field(default_factory=list)
    no_wheel: list[str] = field(default_factory=list)

    @property
    def wheels(self) -> list[WheelPick]:
        return [*self.change, *self.add]

    @property
    def wheel_bytes(self) -> int:
        return sum(item.size or 0 for item in self.wheels)


@dataclass
class PartsPlan:
    """The first decision, before the lock export is downloaded."""

    code: Part
    assets: Part | None
    pylock: Part | None
    runtime: RuntimePlan | None
    current_python: str
    target_python: str | None


def installed_distributions(site_packages: Path) -> dict[str, InstalledDist]:
    """The distributions in one ``site-packages`` folder, by canonical name."""
    out: dict[str, InstalledDist] = {}
    for dist in importlib.metadata.distributions(path=[str(site_packages)]):
        name = dist.metadata.get("Name") if dist.metadata else None
        version = dist.version
        location = getattr(dist, "_path", None)
        if not name or not version or location is None:
            continue
        files = tuple(str(entry).replace("\\", "/") for entry in (dist.files or ()))
        out[canonical_name(name)] = InstalledDist(str(name), str(version), Path(location).name, files)
    return out


def plan_parts(
    manifest: UpdateManifest,
    *,
    current_version: str,
    platform_key: str | None,
    current_python: str,
    current_lock_hash: str | None,
    current_assets_hash: str | None,
) -> PartsPlan:
    """Code always; assets if changed; the lock export if the lock changed."""
    entry = manifest.platform(platform_key) if platform_key else None
    target_python = entry.python if entry else None
    assets = None if current_assets_hash == manifest.assets.content_sha256 else manifest.assets
    runtime: RuntimePlan | None = None
    pylock: Part | None = None
    if manifest.min_source_version and install_layout.compare_versions(
        current_version, manifest.min_source_version
    ) < 0:
        runtime = RuntimePlan("full", f"updates to {manifest.version} start from {manifest.min_source_version}")
    elif target_python is not None and target_python != current_python:
        runtime = RuntimePlan("full", f"Python changes from {current_python} to {target_python}")
    elif manifest.lock_hash and current_lock_hash and manifest.lock_hash == current_lock_hash.lower():
        runtime = RuntimePlan("reuse", "the lock is unchanged")
    elif manifest.pylock is None:
        runtime = RuntimePlan("full", "the release has no lock export")
    else:
        pylock = manifest.pylock
    return PartsPlan(manifest.code, assets, pylock, runtime, current_python, target_python)


def _tag_priority(tags: Iterable[Any]) -> dict[str, int]:
    return {str(tag): index for index, tag in enumerate(tags)}


def _wheel_tags(filename: str) -> list[str]:
    from packaging.utils import parse_wheel_filename

    try:
        return [str(tag) for tag in parse_wheel_filename(filename)[3]]
    except Exception:  # noqa: BLE001 - an unparseable name is not a candidate
        return []


def plan_runtime(
    pylock_text: str,
    *,
    environment: Mapping[str, str],
    tags: Iterable[Any],
    installed: Mapping[str, InstalledDist],
) -> RuntimePlan:
    """Libraries to change, add and remove so the runtime matches the lock."""
    from packaging.markers import Marker
    from packaging.version import InvalidVersion, Version

    try:
        packages = tomllib.loads(pylock_text).get("packages")
    except tomllib.TOMLDecodeError as exc:
        return RuntimePlan("full", f"the lock export is not valid TOML ({exc})")
    if not isinstance(packages, list):
        return RuntimePlan("full", "the lock export lists no packages")
    env = {**dict(environment), "extra": ""}
    priority = _tag_priority(tags)
    wanted: dict[str, dict[str, Any]] = {}
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            continue
        marker = package.get("marker")
        if marker:
            try:
                if not Marker(str(marker)).evaluate(env):
                    continue
            except Exception:  # noqa: BLE001 - an unknown marker is treated as applicable
                pass
        wanted[canonical_name(package["name"])] = package

    plan = RuntimePlan("sync", "the lock changed")
    for name, package in sorted(wanted.items()):
        version = str(package.get("version") or "")
        current = installed.get(name)
        if current is not None:
            try:
                if Version(current.version) == Version(version):
                    continue
            except InvalidVersion:
                if current.version == version:
                    continue
        best: tuple[int, dict[str, Any], str] | None = None
        for wheel in package.get("wheels") or []:
            if not isinstance(wheel, dict) or not isinstance(wheel.get("url"), str):
                continue
            filename = str(wheel.get("name") or urllib.parse.unquote(wheel["url"].rsplit("/", 1)[-1]))
            if not safe_wheel_name(filename):
                continue
            ranks = [priority[tag] for tag in _wheel_tags(filename) if tag in priority]
            if not ranks:
                continue
            rank = min(ranks)
            if best is None or rank < best[0]:
                best = (rank, wheel, filename)
        if best is None:
            plan.no_wheel.append(f"{package['name']} {version}")
            continue
        digest = (best[1].get("hashes") or {}).get("sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
            plan.no_wheel.append(f"{package['name']} {version} (no SHA-256 in the lock)")
            continue
        size = best[1].get("size")
        pick = WheelPick(
            name=str(package["name"]),
            version_from=current.version if current else None,
            version_to=version,
            url=str(best[1]["url"]),
            filename=best[2],
            sha256=digest.lower(),
            size=size if type(size) is int and size > 0 else None,
        )
        (plan.change if current else plan.add).append(pick)
    plan.remove = [
        dist for name, dist in sorted(installed.items()) if name not in wanted and name not in KEEP_DISTS
    ]
    if plan.no_wheel:
        plan.mode = "full"
        plan.reason = "no wheel for " + ", ".join(plan.no_wheel[:5])
    elif not plan.change and not plan.add and not plan.remove:
        plan.mode = "reuse"
        plan.reason = "every library already matches the lock"
    return plan


def prune_paths(
    plan: RuntimePlan, installed: Mapping[str, InstalledDist], *, site_packages: str
) -> set[str]:
    """Files a clone leaves out: those of the libraries the sync changes or removes.

    *site_packages* is the site-packages folder relative to ``python/``
    (``Lib/site-packages`` or ``lib/python3.12/site-packages``). The result
    holds ``/``-separated paths relative to ``python/``; a path ending in ``/``
    leaves out a whole folder (the ``.dist-info``). Leaving them out means the
    sync never uninstalls a file that the running version shares.
    """
    import posixpath

    base = site_packages.replace("\\", "/").strip("/")
    skip: set[str] = set()
    for name in [item.name for item in plan.change] + [item.name for item in plan.remove]:
        dist = installed.get(canonical_name(name))
        if dist is None:
            continue
        for entry in dist.files:
            normalized = entry.replace("\\", "/")
            if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
                continue
            relative = posixpath.normpath(f"{base}/{normalized}")
            if relative.startswith("../") or relative == "..":
                continue
            skip.add(relative)
        skip.add(f"{base}/{dist.dist_info}/")
    return skip


__all__ = [
    "InstalledDist",
    "KEEP_DISTS",
    "PartsPlan",
    "RuntimePlan",
    "WheelPick",
    "canonical_name",
    "installed_distributions",
    "plan_parts",
    "plan_runtime",
    "prune_paths",
    "safe_wheel_name",
]
