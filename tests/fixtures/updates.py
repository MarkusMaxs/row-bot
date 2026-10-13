"""Fakes for background-update tests: a tiny install, release parts, a transport.

Nothing here touches the network or runs a real interpreter. The install is a
handful of files laid out like ``R/versions/<v>/{app,python}``; the transport
serves bytes from a dict (with ``Range`` support and injected failures); the
runner stands in for pip, ``compileall`` and the self-tests.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from row_bot import install_layout
from row_bot.install_layout import InstallKind, InstallLayout
from row_bot.updates.downloader import DownloadError
from row_bot.updates.engine import ReleaseFiles

FEED = "https://github.com/example/row-bot/releases/download"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def site_packages(version_root: Path) -> Path:
    if os.name == "nt":
        return version_root / "python" / "Lib" / "site-packages"
    return version_root / "python" / "lib" / "python3.12" / "site-packages"


def add_dist(site: Path, name: str, version: str, files: Mapping[str, bytes] | None = None) -> None:
    """A minimal installed distribution: its files plus METADATA and RECORD."""
    folder = re.sub(r"[-.]+", "_", name).lower()
    files = dict(files or {f"{folder}/__init__.py": f"VERSION = {version!r}\n".encode()})
    info = site / f"{folder}-{version}.dist-info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n", encoding="utf-8")
    records = []
    for relative, data in files.items():
        target = site / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        records.append(f"{relative},sha256={sha256(data)},{len(data)}")
    records += [f"{info.name}/METADATA,,", f"{info.name}/RECORD,,"]
    (info / "RECORD").write_text("\n".join(records) + "\n", encoding="utf-8")


def make_version(versions: Path, version: str, *, dists: Mapping[str, str], lock: bytes = b"lock-1") -> Path:
    root = versions / version
    app = root / "app"
    (app / "src" / "row_bot").mkdir(parents=True)
    (app / "launcher.py").write_text("# launcher\n", encoding="utf-8")
    (app / "src" / "row_bot" / "version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (app / "uv.lock").write_bytes(lock)
    (app / "static").mkdir()
    (app / "static" / "buddy.json").write_text('{"buddy": 1}\n', encoding="utf-8")
    (app / "sounds").mkdir()
    (app / "sounds" / "ding.wav").write_bytes(b"RIFF-ding")
    python = root / "python"
    if os.name == "nt":
        python.mkdir(parents=True)
        (python / "python.exe").write_bytes(b"MZ-python")
    else:
        (python / "bin").mkdir(parents=True)
        (python / "bin" / "python3").write_bytes(b"\x7fELF-python")
    site = site_packages(root)
    site.mkdir(parents=True)
    for name, dist_version in dists.items():
        add_dist(site, name, dist_version)
    (root / install_layout.VERSION_FILE).write_text(
        json.dumps({"schema": 1, "version": version, "lock_hash": "sha256:" + sha256(lock)}), encoding="utf-8"
    )
    return root


def make_install(
    tmp_path: Path,
    version: str = "5.2.0",
    *,
    dists: Mapping[str, str] | None = None,
    kind: InstallKind | None = None,
) -> InstallLayout:
    """``R/versions/<version>`` (``releases`` for Linux) with ``state.json``."""
    selected = kind or (InstallKind.WINDOWS if os.name == "nt" else InstallKind.LINUX)
    root = tmp_path / "install"
    versions = root / ("releases" if selected is InstallKind.LINUX else "versions")
    version_root = make_version(
        versions, version, dists=dists or {"pip": "26.0", "alpha": "1.0", "beta-lib": "2.0", "gone": "0.1"}
    )
    install_layout.write_state(root, install_layout.new_state(version))
    return InstallLayout(selected, root, version, version_root)


def tar_xz(files: Mapping[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:xz") as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = 0
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def wheel_name(name: str, version: str) -> str:
    return f"{re.sub(r'[-.]+', '_', name)}-{version}-py3-none-any.whl"


def pylock(packages: Mapping[str, str], *, no_wheel: Sequence[str] = (), wheels: Mapping[str, bytes]) -> str:
    lines = ['lock-version = "1.0"', 'created-by = "uv"', ""]
    for name, version in packages.items():
        lines += ["[[packages]]", f'name = "{name}"', f'version = "{version}"']
        if name in no_wheel:
            lines += [f'sdist = {{ url = "https://files.pythonhosted.org/{name}-{version}.tar.gz", '
                      f'hashes = {{ sha256 = "{"0" * 64}" }} }}']
        else:
            filename = wheel_name(name, version)
            data = wheels.get(filename, filename.encode())
            lines += [
                f'wheels = [{{ url = "https://files.pythonhosted.org/packages/{filename}", '
                f'size = {len(data)}, hashes = {{ sha256 = "{sha256(data)}" }} }}]'
            ]
        lines.append("")
    return "\n".join(lines)


@dataclass
class Release:
    """A release's files and what the engine needs to find them."""

    version: str
    blobs: dict[str, bytes]
    manifest: dict[str, Any]
    files: ReleaseFiles
    wheels: dict[str, bytes] = field(default_factory=dict)


def make_release(
    version: str = "5.2.1",
    *,
    code: Mapping[str, bytes] | None = None,
    assets: Mapping[str, bytes] | None = None,
    lock: bytes = b"lock-2",
    packages: Mapping[str, str] | None = None,
    no_wheel: Sequence[str] = (),
    python: str | None = None,
    min_source_version: str | None = None,
    assets_content: str | None = None,
) -> Release:
    code_files = dict(code or {
        "launcher.py": b"# launcher 2\n",
        "src/row_bot/version.py": f'__version__ = "{version}"\n'.encode(),
        "uv.lock": lock,
    })
    asset_files = dict(assets or {"static/buddy.json": b'{"buddy": 2}\n', "sounds/ding.wav": b"RIFF-ding"})
    packages = dict(packages or {"pip": "26.0", "alpha": "1.1", "beta-lib": "2.0", "added": "0.5"})
    packages.pop("pip", None)
    wheels = {wheel_name(name, ver): f"wheel {name} {ver}".encode() for name, ver in packages.items()}
    code_blob = tar_xz(code_files)
    assets_blob = tar_xz(asset_files)
    pylock_blob = pylock(packages, no_wheel=no_wheel, wheels=wheels).encode()
    blobs = {
        f"row-bot-code-{version}.tar.xz": code_blob,
        f"row-bot-assets-{version}.tar.xz": assets_blob,
        f"row-bot-pylock-{version}.toml": pylock_blob,
    }

    def part(name: str) -> dict[str, Any]:
        return {"file": name, "size": len(blobs[name]), "sha256": sha256(blobs[name])}

    if assets_content is None:
        assets_content = _content_hash(asset_files)
    manifest = {
        "schema": 1,
        "version": version,
        "min_source_version": min_source_version,
        "code": part(f"row-bot-code-{version}.tar.xz"),
        "assets": {**part(f"row-bot-assets-{version}.tar.xz"), "content_sha256": assets_content},
        "lock": {"hash": "sha256:" + sha256(lock), "pylock": part(f"row-bot-pylock-{version}.toml")},
        "platforms": {},
    }
    if python is not None:
        manifest["platforms"] = {"windows-x86_64": {"python": python}, "linux-x86_64": {"python": python}}
    manifest_name = f"row-bot-update-{version}.json"
    blobs[manifest_name] = json.dumps(manifest).encode()
    files = ReleaseFiles(
        version=version,
        manifest_url=f"{FEED}/v{version}/{manifest_name}",
        manifest_sha256=sha256(blobs[manifest_name]),
        manifest_size=len(blobs[manifest_name]),
        files={name: (f"{FEED}/v{version}/{name}", len(data)) for name, data in blobs.items()},
        page_url=f"https://github.com/example/row-bot/releases/tag/v{version}",
        installer_url=f"{FEED}/v{version}/Row-Bot-{version}-Windows-x64.exe",
    )
    return Release(version, blobs, manifest, files, wheels)


def _content_hash(files: Mapping[str, bytes]) -> str:
    outer = hashlib.sha256()
    for relative in sorted(files):
        outer.update(relative.encode("utf-8") + b"\0")
        outer.update(hashlib.sha256(files[relative]).digest())
    return outer.hexdigest()


def serve(release: Release) -> dict[str, bytes]:
    """URL -> bytes for :class:`FakeTransport`: parts, manifest and wheels."""
    served = {url: release.blobs[name] for name, (url, _size) in release.files.files.items()}
    for filename, data in release.wheels.items():
        served[f"https://files.pythonhosted.org/packages/{filename}"] = data
    return served


class _Response:
    def __init__(self, status: int, data: bytes, headers: dict[str, str], cut: int | None) -> None:
        self.status = status
        self.headers = headers
        self._data = data
        self._offset = 0
        self._cut = cut

    def read(self, size: int = -1) -> bytes:
        if self._cut is not None and self._offset >= self._cut:
            raise ConnectionResetError("connection dropped")
        end = len(self._data) if size < 0 else self._offset + size
        if self._cut is not None:
            end = min(end, self._cut)
        chunk = self._data[self._offset:end]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        return None


class FakeTransport:
    """Serves bytes by URL, honours ``Range`` and can drop a connection once."""

    def __init__(self, files: Mapping[str, bytes], *, ignore_range: bool = False) -> None:
        self.files = dict(files)
        self.ignore_range = ignore_range
        self.requests: list[tuple[str, str | None]] = []
        self.drop_after: dict[str, int] = {}
        self.fail: dict[str, DownloadError] = {}

    def open(self, url: str, *, headers: Mapping[str, str], timeout: float):  # noqa: ARG002
        self.requests.append((url, headers.get("Range")))
        if url in self.fail:
            raise self.fail.pop(url)
        if url not in self.files:
            raise DownloadError("HTTP 404", retryable=False)
        data = self.files[url]
        start = 0
        status = 200
        response_headers: dict[str, str] = {}
        requested = headers.get("Range")
        if requested and not self.ignore_range:
            start = int(requested.removeprefix("bytes=").split("-")[0])
            status = 206
            response_headers["Content-Range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
        cut = self.drop_after.pop(url, None)
        return _Response(status, data[start:], response_headers, cut)

    def count(self, fragment: str) -> int:
        return sum(1 for url, _range in self.requests if fragment in url)


class FakeRunner:
    """Stands in for pip, compileall and the self-tests of a staged version."""

    def __init__(self, *, fail: str | None = None, wheel_versions: Mapping[str, tuple[str, str]] | None = None) -> None:
        self.fail = fail
        self.commands: list[list[str]] = []
        self.pip_lines: list[str] = []
        self.pip_cwd: Path | None = None
        self.wheel_versions = dict(wheel_versions or {})

    def __call__(self, command, *, env, cwd, timeout, log):  # noqa: ANN001, ARG002
        command = [str(part) for part in command]
        self.commands.append(command)
        label = self.label(command)
        if label == self.fail:
            return 1
        if label == "pip":
            requirements = Path(command[command.index("-r") + 1])
            python = Path(command[0])
            root = python.parent if python.name == "python.exe" else python.parent.parent
            site = site_packages(root.parent)
            self.pip_cwd = Path(cwd) if cwd else None
            self.pip_lines = requirements.read_text(encoding="utf-8").splitlines()
            for line in self.pip_lines:
                wheel = (self.pip_cwd or Path.cwd()) / line.split(" --hash=")[0]
                assert wheel.is_file(), line  # pip resolves the line from its working folder
                stem = wheel.name.split("-")
                add_dist(site, stem[0].replace("_", "-"), stem[1])
        assert env.get("PYTHONNOUSERSITE") == "1"
        return 0

    @staticmethod
    def label(command: Sequence[str]) -> str:
        if "pip" in command:
            return "pip"
        if "compileall" in command:
            return "compile"
        if any(part.endswith("verify_runtime_dependencies.py") for part in command):
            return "dependencies"
        if "-c" in command:
            return "server_import"
        return "other"

    def labels(self) -> list[str]:
        return [self.label(command) for command in self.commands]


def tree_fingerprint(root: Path) -> dict[str, tuple[str, int]]:
    """Every file's SHA-256 and inode, to prove a tree was not written to."""
    out: dict[str, tuple[str, int]] = {}
    for folder, _dirs, names in os.walk(root):
        for name in names:
            path = Path(folder) / name
            out[path.relative_to(root).as_posix()] = (sha256(path.read_bytes()), path.stat().st_ino)
    return out
