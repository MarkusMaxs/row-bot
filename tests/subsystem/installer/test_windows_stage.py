"""The Windows installer stage reaches the signing machine whole, and only for its run.

release.yml packs the stage ISCC needs (`scripts/windows_stage.py pack`) with
the run id and commit that built it. `scripts/release_windows.ps1` unpacks and
counts it, and reuses an unpacked stage only for the same run and commit, so a
dry run's stage is never signed for a real release.
"""
from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

import scripts.windows_stage as stage

pytestmark = [pytest.mark.subsystem, pytest.mark.installer]

RUN, SHA = "38190884972", "da126816183b87afe886cc1f93073cddc31452fd"
OTHER_SHA = "81f0c32e2e120ec3c493d426fa3ec83417da3f2d"


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


@pytest.fixture
def archive(tmp_path: Path, checkout: Path) -> Path:
    path = tmp_path / "stage.tar"
    stage.pack(checkout, path, run_id=RUN, sha=SHA)
    return path


def test_the_stage_unpacks_whole_with_its_empty_folders(tmp_path, archive) -> None:
    unpacked = stage.unpack(archive, tmp_path / "s", run_id=RUN, sha=SHA)

    assert (unpacked["files"], unpacked["run_id"], unpacked["sha"]) == (8, RUN, SHA)
    assert (tmp_path / "s/installer/build/python/Lib/empty").is_dir()
    assert (tmp_path / "s/installer/row_bot_setup.iss").read_bytes() == b"installer/row_bot_setup.iss"
    assert not (tmp_path / "s/installer/build/unrelated.obj").exists()
    assert stage.check(tmp_path / "s", run_id=RUN, sha=SHA) is None


def test_a_stage_from_another_run_or_commit_is_refused(tmp_path, archive) -> None:
    with pytest.raises(stage.StageError, match="built by run"):
        stage.unpack(archive, tmp_path / "s", run_id="1", sha=SHA)
    with pytest.raises(stage.StageError, match="built by run"):
        stage.unpack(archive, tmp_path / "s", run_id=RUN, sha=OTHER_SHA)
    # Nothing that failed a check is ever marked usable.
    assert stage.check(tmp_path / "s", run_id=RUN, sha=SHA) == "it was never unpacked and checked completely"


def test_a_short_stage_is_refused_and_not_reused(tmp_path, archive) -> None:
    short = tmp_path / "short.tar"
    with tarfile.open(archive) as source, tarfile.open(short, "w") as target:
        for member in source.getmembers():
            if not member.name.endswith("six.py"):
                target.addfile(member, source.extractfile(member) if member.isfile() else None)

    with pytest.raises(stage.StageError, match="the unpacked stage has"):
        stage.unpack(short, tmp_path / "s", run_id=RUN, sha=SHA)
    # The stage.json it extracted does not make a re-run skip the check.
    assert (tmp_path / "s" / stage.STAGE_JSON).is_file()
    assert stage.check(tmp_path / "s", run_id=RUN, sha=SHA) is not None


@pytest.mark.parametrize(
    ("run_id", "sha", "change", "reason"),
    [
        (RUN, SHA, None, None),
        ("38187043753", SHA, None, "belongs to run 38190884972"),
        (RUN, OTHER_SHA, None, "belongs to run"),
        (RUN, SHA, "installer/build/python/Lib/site-packages/six.py", "files changed"),
        (RUN, SHA, "installer/build/Row-Bot.exe", None),  # signed in place: still the same stage
    ],
)
def test_an_unpacked_stage_is_reused_only_for_its_own_run(tmp_path, archive, run_id, sha, change, reason) -> None:
    stage.unpack(archive, tmp_path / "s", run_id=RUN, sha=SHA)
    if change:
        (tmp_path / "s" / change).write_bytes(b"longer than before, as a signature makes it")

    found = stage.check(tmp_path / "s", run_id=run_id, sha=sha)

    assert found is None if reason is None else reason in found


def test_a_stage_needs_its_run_and_commit_and_the_launcher(tmp_path, checkout) -> None:
    with pytest.raises(stage.StageError, match="run id and the full commit"):
        stage.pack(checkout, tmp_path / "stage.tar", run_id="", sha=SHA)
    (checkout / "installer/build/Row-Bot.exe").unlink()
    with pytest.raises(stage.StageError, match="Row-Bot.exe is missing"):
        stage.pack(checkout, tmp_path / "stage.tar", run_id=RUN, sha=SHA)


def test_the_packed_stage_names_its_run(tmp_path, archive) -> None:
    with tarfile.open(archive) as tar:
        packed = json.loads(tar.extractfile(stage.STAGE_JSON).read())

    assert (packed["run_id"], packed["sha"], packed["files"]) == (RUN, SHA, 8)
