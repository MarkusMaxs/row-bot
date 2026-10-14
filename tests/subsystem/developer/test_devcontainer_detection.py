from __future__ import annotations



def _fresh_modules(tmp_path, reload_for_data_dir):

    return reload_for_data_dir(tmp_path / "data", "row_bot.developer.devcontainer")[0]


def test_devcontainer_detection_absent(tmp_path, reload_for_data_dir):
    devcontainer = _fresh_modules(tmp_path, reload_for_data_dir)
    repo = tmp_path / "repo"
    repo.mkdir()

    info = devcontainer.detect_devcontainer(str(repo))

    assert info.present is False
    assert "No devcontainer" in info.message


def test_devcontainer_detection_reads_basic_config(tmp_path, reload_for_data_dir):
    devcontainer = _fresh_modules(tmp_path, reload_for_data_dir)
    repo = tmp_path / "repo"
    config_dir = repo / ".devcontainer"
    config_dir.mkdir(parents=True)
    (config_dir / "devcontainer.json").write_text(
        '{"name": "Row-Bot Dev", "image": "mcr.microsoft.com/devcontainers/python:1-3.12"}',
        encoding="utf-8",
    )

    info = devcontainer.detect_devcontainer(str(repo))

    assert info.present is True
    assert info.name == "Row-Bot Dev"
    assert info.image.startswith("mcr.microsoft.com")


def test_devcontainer_docker_detection_handles_missing_binary(tmp_path, monkeypatch, reload_for_data_dir):
    devcontainer = _fresh_modules(tmp_path, reload_for_data_dir)
    monkeypatch.setattr(devcontainer, "resolve_docker", lambda: "")

    assert devcontainer.detect_docker() is False
