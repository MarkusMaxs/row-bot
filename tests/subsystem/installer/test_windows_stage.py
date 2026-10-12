"""The Windows installer stage reaches the signing machine whole.

release.yml packs the stage ISCC needs (`scripts/windows_stage.py pack`), and
`scripts/release_windows.ps1` unpacks it and checks it against the count packed
with it, so a short stage is never packed into a signed installer.
"""
from __future__ import annotations

import tarfile
from pathlib import Path

import pytest

import scripts.windows_stage as stage

pytestmark = [pytest.mark.subsystem, pytest.mark.installer]


def _write(path: Path, data: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    for name in stage.FILES:
        _write(root / name, name.encode())
    _write(root / "installer/build/app/launcher.py", b"print('hi')\n")
    _write(root / "installer/build/python/python.exe", b"MZ" + b"\0" * 64)
    _write(root / "installer/build/python/Lib/site-packages/six.py", b"# six\n")
    (root / "installer/build/python/Lib/empty").mkdir(parents=True)
    _write(root / "installer/build/unrelated.obj")  # build leftovers stay behind
    return root


def test_the_stage_unpacks_whole_with_its_empty_folders(tmp_path, checkout) -> None:
    archive = tmp_path / "stage.tar"
    packed = stage.pack(checkout, archive)

    unpacked = stage.unpack(archive, tmp_path / "s")

    assert packed == unpacked == {"files": 8, "bytes": packed["bytes"]}
    assert (tmp_path / "s/installer/build/python/Lib/empty").is_dir()
    assert (tmp_path / "s/installer/row_bot_setup.iss").read_bytes() == b"installer/row_bot_setup.iss"
    assert not (tmp_path / "s/installer/build/unrelated.obj").exists()


def test_a_short_stage_is_refused(tmp_path, checkout) -> None:
    archive = tmp_path / "stage.tar"
    stage.pack(checkout, archive)
    short = tmp_path / "short.tar"
    with tarfile.open(archive) as source, tarfile.open(short, "w") as target:
        for member in source.getmembers():
            if not member.name.endswith("six.py"):
                target.addfile(member, source.extractfile(member) if member.isfile() else None)

    with pytest.raises(stage.StageError, match="the unpacked stage has"):
        stage.unpack(short, tmp_path / "s")


def test_a_stage_without_the_launcher_is_not_packed(tmp_path, checkout) -> None:
    (checkout / "installer/build/Row-Bot.exe").unlink()

    with pytest.raises(stage.StageError, match="Row-Bot.exe is missing"):
        stage.pack(checkout, tmp_path / "stage.tar")
