"""The release body's SHA-256 block lists every file the updater may download.

`scripts/append_sha_manifest.py` writes it three ways: the local publish step
writes the whole block from the final files, `container.yml` adds its digest
file after publishing, and `update-manifest.yml` is a backstop that fills a
missing block but never changes a listed hash.
"""
from __future__ import annotations

import json

import pytest

import scripts.append_sha_manifest as block
from row_bot import updater

pytestmark = [pytest.mark.subsystem, pytest.mark.installer, pytest.mark.platform]

EXE = "Row-Bot-5.2.0-Windows-x64.exe"
UPDATE = "row-bot-update-5.2.0.json"
CONTAINER = "row-bot-container-5.2.0.json"
NOTES = "## v5.2.0 - Background Updates\n\nUpdates download in the background.\n"


def test_the_offline_block_is_exactly_the_listing_and_reads_back_in_the_client(tmp_path) -> None:
    listing = tmp_path / "listing.json"
    listing.write_text(json.dumps([{"name": EXE, "size": 1, "sha256": "A" * 64},
                                   {"name": UPDATE, "size": 2, "sha256": "b" * 64}]), encoding="utf-8")
    body = tmp_path / "body.md"
    body.write_text(NOTES, encoding="utf-8")
    written = tmp_path / "new.md"

    assert block.main(["--listing", str(listing), "--body-file", str(body), "--write-body", str(written)]) == 0
    text = written.read_text(encoding="utf-8")
    assert text.startswith(NOTES)
    assert updater.parse_manifest(text) == {EXE: "a" * 64, UPDATE: "b" * 64}

    # Writing it again (an earlier block in the body) gives the same body.
    assert block.main(["--listing", str(listing), "--body-file", str(written), "--write-body", str(written)]) == 0
    assert written.read_text(encoding="utf-8") == text


def test_add_updates_its_files_and_the_backstop_never_changes_a_listed_hash() -> None:
    existing = {EXE: "a" * 64, UPDATE: "b" * 64}

    assert block.merged_hashes(existing, {CONTAINER: "c" * 64, UPDATE: "d" * 64}, "add") == {
        EXE: "a" * 64, UPDATE: "d" * 64, CONTAINER: "c" * 64}
    assert block.merged_hashes(existing, {EXE: "a" * 64, CONTAINER: "c" * 64}, "backstop") == {
        **existing, CONTAINER: "c" * 64}
    assert block.merged_hashes(None, {EXE: "a" * 64}, "backstop") == {EXE: "a" * 64}
    with pytest.raises(block.ManifestConflict, match=EXE):
        block.merged_hashes(existing, {EXE: "f" * 64}, "backstop")
    assert block.merged_hashes(existing, {EXE: "f" * 64}, "replace") == {EXE: "f" * 64}


class FakeReleaseApi:
    def __init__(self, body: str) -> None:
        self.body = body
        self.patches: list[dict] = []

    def __call__(self, url, token, method="GET", payload=None):
        assert token == "token"
        if method == "PATCH":
            assert url.endswith("/releases/7")
            self.patches.append(payload)
            self.body = payload["body"]
            return {}
        assert url.endswith("/releases/tags/v5.2.0")
        return {"id": 7, "body": self.body}


@pytest.fixture
def release_files(tmp_path):
    files = {}
    for name, data in ((EXE, b"installer"), (UPDATE, b"{}"), (CONTAINER, b'{"image": "x"}')):
        files[name] = tmp_path / name
        files[name].write_bytes(data)
    return files


def _run(monkeypatch, api, *args):
    monkeypatch.setattr(block, "_api", api)
    return block.main(["--tag", "v5.2.0", "--token", "token", "--repo", "owner/row-bot", *args])


def test_the_backstop_leaves_a_correct_block_alone(monkeypatch, release_files) -> None:
    hashes = {name: block.sha256_of(path) for name, path in release_files.items() if name != CONTAINER}
    api = FakeReleaseApi(block.merge_into_body(NOTES, block.build_manifest_block(hashes)))

    assert _run(monkeypatch, api, "--backstop", "--files", str(release_files[EXE]), str(release_files[UPDATE])) == 0
    assert api.patches == []

    # A file the block does not list yet is added; the listed ones stay.
    assert _run(monkeypatch, api, "--backstop", "--files", *map(str, release_files.values())) == 0
    assert updater.parse_manifest(api.body) == {name: block.sha256_of(path) for name, path in release_files.items()}
    assert api.body.startswith(NOTES)


def test_the_backstop_refuses_to_overwrite_a_block_whose_file_changed(monkeypatch, release_files, capsys) -> None:
    api = FakeReleaseApi(block.merge_into_body(NOTES, block.build_manifest_block({EXE: "a" * 64})))

    assert _run(monkeypatch, api, "--backstop", "--files", str(release_files[EXE])) == 1
    assert api.patches == []
    assert "no longer match" in capsys.readouterr().out


def test_a_release_without_a_block_gets_one(monkeypatch, release_files) -> None:
    api = FakeReleaseApi(NOTES)

    assert _run(monkeypatch, api, "--backstop", "--files", str(release_files[EXE])) == 0
    assert updater.parse_manifest(api.body) == {EXE: block.sha256_of(release_files[EXE])}


def test_container_adds_its_digest_file_to_the_published_block(monkeypatch, release_files) -> None:
    api = FakeReleaseApi(block.merge_into_body(NOTES, block.build_manifest_block({EXE: "a" * 64})))

    assert _run(monkeypatch, api, "--add", "--files", str(release_files[CONTAINER])) == 0
    assert updater.parse_manifest(api.body) == {EXE: "a" * 64, CONTAINER: block.sha256_of(release_files[CONTAINER])}


def test_the_backstop_never_vouches_for_a_stray_file(monkeypatch, release_files, tmp_path, capsys) -> None:
    stray = tmp_path / "Row-Bot-5.2.0-Windows-arm64.exe"
    stray.write_bytes(b"not ours")
    api = FakeReleaseApi(NOTES)

    assert _run(monkeypatch, api, "--backstop", "--only-release-files", "--files", str(release_files[EXE]),
                str(stray)) == 1
    assert api.patches == []
    assert "Row-Bot-5.2.0-Windows-arm64.exe" in capsys.readouterr().out
    assert _run(monkeypatch, api, "--backstop", "--only-release-files", "--files",
                *map(str, release_files.values())) == 0

