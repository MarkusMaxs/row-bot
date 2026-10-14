"""Settings › Data backup and restore through the API: any signed-in device, reviewed restores."""
from __future__ import annotations

import io
import json
import random
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

import pytest
from starlette.middleware.gzip import GZipMiddleware

from row_bot import profile_restore
from row_bot.application import client_data_backup as data
from row_bot.application import profile_backup
from tests.subsystem.client_protocol.test_protocol_security import _native_proof, bootstrap, client_app

pytestmark = pytest.mark.subsystem

UPLOADS = "/api/v1/data/backup/uploads"
DOWNLOAD = "/api/v1/data/backup/download"


@pytest.fixture
def places(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "tools_config.json").write_text('{"enabled": ["web_search"]}', encoding="utf-8")
    (profile / "api_keys.json").write_text('{"openai": "sk-SYNTHETIC-never-archived"}', encoding="utf-8")
    backups = tmp_path / "Row-Bot" / "Backups"
    backups.mkdir(parents=True)
    monkeypatch.setattr(data, "_data_dir", lambda: profile)
    monkeypatch.setattr(data, "_backups_folder", lambda: backups)
    monkeypatch.setattr(data, "_folder_words", lambda: "Row-Bot › Backups")
    data._JOB.clear()
    data._REVIEWS.clear()
    data.close_uploads()
    return profile, backups


def _command(client, headers, action, **extra):
    identity = str(uuid4())
    return client.post("/api/v1/data/backup/commands",
                       headers={**headers, "Idempotency-Key": identity},
                       json={"command_id": identity, "client_session_id": headers["X-Client-Session"],
                             "action": action, **extra})


def _settled(client, headers):
    """The page's state once the background backup or restore has finished.

    Waits on the job's thread instead of polling: fast polls ran into the
    query lane's rate limit when a job took over half a second.
    """
    for thread in threading.enumerate():
        if thread.name.startswith("row-bot-data-"):
            thread.join(timeout=60)
    answer = client.get("/api/v1/data/backup", headers=headers)
    assert answer.status_code == 200, answer.text
    state = answer.json()
    assert not state["job"] or state["job"]["status"] != "running", "the job did not finish"
    return state


def _pick(client, proof, path: Path) -> str:
    picked = client.post("/api/v1/native/selections/complete", headers={"Origin": "http://localhost"},
                         json={**proof, "selection_kind": "file", "intent_id": str(uuid4()),
                               "intent": "restore_backup", "conversation_id": None,
                               "destination": "data-restore", "path": str(path)})
    assert picked.status_code == 200, picked.text
    assert str(path) not in picked.text
    return picked.json()["reference"]


def test_back_up_then_restore_on_the_next_start_only_after_confirming(places):
    profile, backups = places
    client, _service, _active = client_app()
    with client:
        proof, headers = _native_proof(client)
        state = client.get("/api/v1/data/backup", headers=headers).json()
        assert state["reveal_available"] is True and state["last_backup_at"] is None
        started = _command(client, headers, "backup")
        assert started.status_code == 200, started.text
        assert started.json()["status"] == "accepted"
        state = _settled(client, headers)
        assert state["job"]["status"] == "completed"
        archive = backups / state["last_backup_name"]
        assert archive.is_file() and state["folder"] == "Row-Bot › Backups"
        assert "api_keys.json" not in zipfile.ZipFile(archive).namelist()

        reviewed = _command(client, headers, "inspect_restore", file_grant=_pick(client, proof, archive))
        assert reviewed.status_code == 200, reviewed.text
        review = reviewed.json()["review"]
        assert review["source_name"] == archive.name and len(review["review_id"]) == 64
        # Looking at a backup changes nothing.
        assert _settled(client, headers)["pending_restore"] is None

        wrong = _command(client, headers, "restore", review_id="0" * 64)
        assert wrong.status_code == 409 and wrong.json()["code"] == "backup_review_expired"
        confirmed = _command(client, headers, "restore", review_id=review["review_id"])
        assert confirmed.status_code == 200, confirmed.text
        state = _settled(client, headers)
        assert state["pending_restore"]["source_name"] == archive.name
        # One confirmation, one use.
        again = _command(client, headers, "restore", review_id=review["review_id"])
        assert again.status_code == 409
        cancelled = _command(client, headers, "cancel_restore")
        assert cancelled.json()["state"]["pending_restore"] is None
        assert (profile / "tools_config.json").read_text(encoding="utf-8") == '{"enabled": ["web_search"]}'


def test_a_foreign_archive_is_refused_before_anything_is_staged(places, tmp_path):
    profile, _backups = places
    foreign = tmp_path / "photos.zip"
    with zipfile.ZipFile(foreign, "w") as bundle:
        bundle.writestr("holiday.txt", "not a backup")
    client, _service, _active = client_app()
    with client:
        proof, headers = _native_proof(client)
        refused = _command(client, headers, "inspect_restore", file_grant=_pick(client, proof, foreign))
        assert refused.status_code == 422 and refused.json()["code"] == "backup_not_row_bot"
        assert client.get("/api/v1/data/backup", headers=headers).json()["pending_restore"] is None
    assert not (profile / ".restore-pending").exists()


def test_another_device_backs_up_and_reads_the_state_but_not_the_file_manager(places, monkeypatch):
    """One owner: a paired phone or a server session backs up too. Show in folder
    opens the file manager on the computer Row-Bot runs on, so it stays with the
    desktop app, and a pick from the desktop app's picker can't be forged."""
    _profile, backups = places
    opened = []
    monkeypatch.setattr(data, "reveal_last", lambda: opened.append("reveal") or True)
    client, _service, active = client_app(remote=True)
    with client:
        _handshake, headers = bootstrap(client)
        state = client.get("/api/v1/data/backup", headers=headers)
        assert state.status_code == 200, state.text
        assert state.json()["reveal_available"] is False and state.json()["folder"] == "Row-Bot › Backups"
        started = _command(client, headers, "backup")
        assert started.status_code == 200, started.text
        assert started.json()["status"] == "accepted"
        settled = _settled(client, headers)
        assert settled["job"]["status"] == "completed"
        assert (backups / settled["last_backup_name"]).is_file()
        reveal = _command(client, headers, "reveal")
        assert reveal.status_code == 409 and reveal.json()["code"] == "desktop_only"
        # A pick comes from the desktop app's own picker; nothing else names a file.
        forged = _command(client, headers, "inspect_restore", file_grant="not-a-pick")
        assert forged.status_code == 409 and forged.json()["code"] == "capability_revoked"
        active["value"] = False
        assert _command(client, headers, "backup").status_code == 401
    assert opened == []


def _backed_up(client, headers, backups: Path) -> Path:
    started = _command(client, headers, "backup")
    assert started.status_code == 200, started.text
    state = _settled(client, headers)
    assert state["job"]["status"] == "completed"
    return backups / state["last_backup_name"]


def _record_last_backup(profile: Path, name: str) -> None:
    (profile / profile_backup.STATE_FILE).write_text(
        json.dumps({"last_backup_at": "2026-10-11T09:00:00", "name": name}), encoding="utf-8")


def test_another_device_downloads_the_last_backup_it_was_shown(places):
    """A server or Docker install has no file manager to show: the archive comes to the device."""
    _profile, backups = places
    client, _service, active = client_app(remote=True)
    # As the app serves it: gzip outermost. An archive is already compressed and keeps its length.
    client.app.add_middleware(GZipMiddleware)
    with client:
        _handshake, headers = bootstrap(client)
        archive = _backed_up(client, headers, backups)
        got = client.get(DOWNLOAD, params={"name": archive.name}, headers=headers)
        assert got.status_code == 200, got.text
        assert got.content == archive.read_bytes()
        assert "api_keys.json" not in zipfile.ZipFile(io.BytesIO(got.content)).namelist()
        assert got.headers["content-type"] == "application/zip"
        assert got.headers["content-length"] == str(archive.stat().st_size)
        assert got.headers.get("content-encoding", "identity") == "identity"
        assert got.headers["cache-control"] == "no-store"
        assert got.headers["content-disposition"] == (
            f'attachment; filename="{archive.name}"; filename*=UTF-8\'\'{quote(archive.name, safe="")}')
        # Only the backup on screen: a newer one is never sent under an older name.
        older = client.get(DOWNLOAD, params={"name": "Row-Bot backup 2001-01-01 0000.zip"}, headers=headers)
        assert older.status_code == 404 and older.json()["code"] == "backup_unavailable"
        active["value"] = False
        signed_out = client.get(DOWNLOAD, headers=headers)
        assert signed_out.status_code == 401 and signed_out.json()["code"] == "authentication_required"


def test_a_downloaded_name_is_sent_safely_in_both_header_forms(places):
    profile, backups = places
    archive = backups / "Sauvegarde été 2026.zip"
    archive.write_bytes(b"PK\x05\x06" + bytes(18))
    _record_last_backup(profile, archive.name)
    client, _service, _active = client_app(remote=True)
    with client:
        _handshake, headers = bootstrap(client)
        got = client.get(DOWNLOAD, headers=headers)
        assert got.status_code == 200, got.text
        assert got.headers["content-disposition"] == (
            "attachment; filename=\"Sauvegarde _t_ 2026.zip\"; "
            "filename*=UTF-8''Sauvegarde%20%C3%A9t%C3%A9%202026.zip")


@pytest.mark.parametrize("escape", ["parent", "absolute", "symlink"])
def test_a_download_never_leaves_the_backups_folder(places, escape):
    profile, backups = places
    elsewhere = backups.parent / "elsewhere.zip"
    elsewhere.write_bytes(b"never downloaded")
    if escape == "parent":
        name = "../elsewhere.zip"
    elif escape == "absolute":
        name = str(elsewhere)
    else:
        link = backups / "Row-Bot backup 2026-10-11 0900.zip"
        try:
            link.symlink_to(elsewhere)
        except OSError:
            pytest.skip("creating a symbolic link needs extra rights here")
        name = link.name
    _record_last_backup(profile, name)
    client, _service, _active = client_app(remote=True)
    with client:
        _handshake, headers = bootstrap(client)
        got = client.get(DOWNLOAD, headers=headers)
        assert got.status_code == 404 and got.json()["code"] == "backup_unavailable"
        assert b"never downloaded" not in got.content


def test_signing_out_mid_download_stops_the_archive(places, monkeypatch):
    profile, backups = places
    archive = backups / "Row-Bot backup 2026-10-11 0900.zip"
    archive.write_bytes(random.Random(7).randbytes(data.CHUNK_BYTES * 2 + 512))
    _record_last_backup(profile, archive.name)
    client, _service, active = client_app(remote=True)
    opened = data.open_last_backup

    class SignsOutAfterOneRead:
        def __init__(self, source):
            self.source = source

        def read(self, count):
            part = self.source.read(count)
            active["value"] = False  # signed out (or the device removed) while the first chunk is on its way
            return part

        def close(self):
            self.source.close()

    def opening(expected=None):
        source, size, name = opened(expected)
        return SignsOutAfterOneRead(source), size, name

    monkeypatch.setattr(data, "open_last_backup", opening)
    with client:
        _handshake, headers = bootstrap(client)
        got = client.get(DOWNLOAD, headers=headers)
        assert got.status_code == 200
        assert int(got.headers["content-length"]) == archive.stat().st_size
        assert len(got.content) == data.CHUNK_BYTES
    archive.unlink()  # nothing holds the archive open afterwards


def _begin(client, headers, *, size: int, name: str = "Phone backup.zip"):
    return client.post(UPLOADS, headers=headers,
                       json={"client_session_id": headers["X-Client-Session"], "name": name, "size_bytes": size})


def _chunk(client, headers, upload: str, offset: int, body: bytes):
    return client.put(f"{UPLOADS}/{upload}/chunks", params={"offset": offset}, headers=headers, content=body)


def _uploaded(client, headers, archive: bytes) -> str:
    begun = _begin(client, headers, size=len(archive))
    assert begun.status_code == 200, begun.text
    upload, step = begun.json()["upload_id"], begun.json()["chunk_bytes"]
    for offset in range(0, len(archive), step):
        sent = _chunk(client, headers, upload, offset, archive[offset:offset + step])
        assert sent.status_code == 200, sent.text
    return upload


def test_another_device_restores_by_sending_the_archive_then_confirming(places, tmp_path):
    """No desktop picker on a phone or a server install: the archive is sent in chunks,
    checked like a picked one, and staged for the next start only once confirmed."""
    profile, backups = places
    (profile / "media").mkdir()
    (profile / "media" / "photo.bin").write_bytes(random.Random(3).randbytes(1_500_000))
    made = profile_backup.create_backup(profile, tmp_path / "made elsewhere")
    archive = Path(made["path"]).read_bytes()
    staging = profile / profile_restore.UPLOADS_DIR
    client, _service, _active = client_app(remote=True)
    with client:
        _handshake, headers = bootstrap(client)
        begun = _begin(client, headers, size=len(archive))
        assert begun.status_code == 200, begun.text
        upload, step = begun.json()["upload_id"], begun.json()["chunk_bytes"]
        assert begun.json()["received_bytes"] == 0 and len(archive) > step
        # In order, each chunk once: a skipped chunk is refused, a retried one is answered.
        skipped = _chunk(client, headers, upload, step, archive[step:2 * step])
        assert skipped.status_code == 422 and skipped.json()["code"] == "invalid_command"
        assert _chunk(client, headers, upload, 0, archive[:step]).json()["received_bytes"] == step
        assert _chunk(client, headers, upload, 0, archive[:step]).json()["received_bytes"] == step
        changed = _chunk(client, headers, upload, 0, bytes(step))
        assert changed.status_code == 409 and changed.json()["code"] == "revision_conflict"
        early = _command(client, headers, "inspect_restore", upload_id=upload)
        assert early.status_code == 409 and early.json()["code"] == "upload_incomplete"
        for offset in range(step, len(archive), step):
            assert _chunk(client, headers, upload, offset, archive[offset:offset + step]).status_code == 200
        status = client.get(f"{UPLOADS}/{upload}", headers=headers).json()
        assert status["received_bytes"] == status["size_bytes"] == len(archive)
        # It waits only in the data folder's private staging area, and a backup never holds it.
        assert [path.read_bytes() for path in staging.iterdir()] == [archive]
        newer = _backed_up(client, headers, backups)
        assert not [name for name in zipfile.ZipFile(newer).namelist() if profile_restore.UPLOADS_DIR in name]

        both = _command(client, headers, "inspect_restore", upload_id=upload, file_grant="a-pick")
        assert both.status_code == 422 and both.json()["code"] == "invalid_command"
        reviewed = _command(client, headers, "inspect_restore", upload_id=upload)
        assert reviewed.status_code == 200, reviewed.text
        review = reviewed.json()["review"]
        assert review["source_name"] == "Phone backup.zip" and review["files"] == made["files"]
        assert _settled(client, headers)["pending_restore"] is None  # looking changes nothing

        confirmed = _command(client, headers, "restore", review_id=review["review_id"])
        assert confirmed.status_code == 200 and confirmed.json()["status"] == "accepted", confirmed.text
        state = _settled(client, headers)
        assert state["job"]["status"] == "completed"
        assert state["pending_restore"]["source_name"] == "Phone backup.zip"
        # Staged for the next start; the sent archive is deleted.
        assert list(staging.iterdir()) == []
        assert client.get(f"{UPLOADS}/{upload}", headers=headers).json()["code"] == "upload_expired"
        assert not (profile / profile_restore.PENDING_DIR / "files" / profile_restore.UPLOADS_DIR).exists()
    # Nothing is replaced before the next start.
    assert (profile / "tools_config.json").read_text(encoding="utf-8") == '{"enabled": ["web_search"]}'


def test_a_restore_upload_is_bounded_and_belongs_to_the_window_that_sent_it(places, monkeypatch):
    profile, _backups = places
    staging = profile / profile_restore.UPLOADS_DIR
    client, _service, active = client_app(remote=True)
    with client:
        _handshake, headers = bootstrap(client)
        _other_handshake, other = bootstrap(client)
        huge = _begin(client, headers, size=profile_backup.MAX_BYTES + 1)
        assert huge.status_code == 413 and huge.json()["code"] == "backup_too_large"
        assert _begin(client, headers, size=10, name="../escape.zip").status_code == 422
        with monkeypatch.context() as patch:
            patch.setattr(data.shutil, "disk_usage", lambda folder: SimpleNamespace(free=9))
            full = _begin(client, headers, size=10)
            assert full.status_code == 507 and full.json()["code"] == "backup_storage_full"
        assert list(staging.iterdir()) == []

        upload = _begin(client, headers, size=data.CHUNK_BYTES + 1).json()["upload_id"]
        oversized = _chunk(client, headers, upload, 0, bytes(data.CHUNK_BYTES + 1))
        assert oversized.status_code == 413 and oversized.json()["code"] == "payload_too_large"
        # Another window can't see, add to, check or cancel it.
        for answer in (client.get(f"{UPLOADS}/{upload}", headers=other),
                       _chunk(client, other, upload, 0, bytes(data.CHUNK_BYTES)),
                       _command(client, other, "inspect_restore", upload_id=upload),
                       client.delete(f"{UPLOADS}/{upload}", headers=other)):
            assert answer.status_code == 404 and answer.json()["code"] == "not_found"
        assert client.get(f"{UPLOADS}/{upload}", headers=headers).json()["received_bytes"] == 0
        # Signed out: nothing more is taken.
        active["value"] = False
        assert _chunk(client, headers, upload, 0, bytes(data.CHUNK_BYTES)).status_code == 401
        assert _begin(client, headers, size=10).status_code == 401
        assert [path.stat().st_size for path in staging.iterdir()] == [0]
    # Upload ids don't outlive the server, and neither do their files.
    assert list(staging.iterdir()) == []


def test_a_refused_cancelled_or_expired_upload_leaves_nothing_behind(places, monkeypatch, tmp_path):
    profile, _backups = places
    staging = profile / profile_restore.UPLOADS_DIR
    clock = [1000.0]
    monkeypatch.setattr(data, "_now", lambda: clock[0])
    foreign = io.BytesIO()
    with zipfile.ZipFile(foreign, "w") as bundle:
        bundle.writestr("holiday.txt", "not a backup")
    archive = Path(profile_backup.create_backup(profile, tmp_path / "made")["path"]).read_bytes()
    client, _service, _active = client_app(remote=True)
    with client:
        _handshake, headers = bootstrap(client)
        # Not a Row-Bot backup: refused, and deleted at once.
        refused = _command(client, headers, "inspect_restore", upload_id=_uploaded(client, headers, foreign.getvalue()))
        assert refused.status_code == 422 and refused.json()["code"] == "backup_not_row_bot"
        assert list(staging.iterdir()) == []

        # Cancelled after its check: the file and its review both go.
        upload = _uploaded(client, headers, archive)
        review = _command(client, headers, "inspect_restore", upload_id=upload).json()["review"]
        cancelled = client.delete(f"{UPLOADS}/{upload}", headers=headers)
        assert cancelled.status_code == 200 and cancelled.json() == {"cancelled": True}
        assert list(staging.iterdir()) == []
        stale = _command(client, headers, "restore", review_id=review["review_id"])
        assert stale.status_code == 409 and stale.json()["code"] == "backup_review_expired"

        # Checked but not confirmed: it goes with its review.
        checked = _command(client, headers, "inspect_restore", upload_id=_uploaded(client, headers, archive))
        assert checked.status_code == 200, checked.text
        clock[0] += 601
        data.expire_uploads()
        assert list(staging.iterdir()) == []

        # Left half sent: gone once its time is up, as is a file an earlier run left behind.
        upload = _begin(client, headers, size=len(archive)).json()["upload_id"]
        (staging / "upload-abcd1234.zip").write_bytes(b"from before a restart")
        clock[0] += 1801
        data.expire_uploads()
        assert list(staging.iterdir()) == []
        assert client.get(f"{UPLOADS}/{upload}", headers=headers).json()["code"] == "upload_expired"
