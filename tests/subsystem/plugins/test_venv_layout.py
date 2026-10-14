"""Copied venv construction stays compatible with strict link-free readiness."""
from __future__ import annotations

import os
import sys
import venv
from uuid import uuid4

import pytest

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]


@pytest.mark.parametrize("has_ensurepip", [True, False])
def test_environments_need_ensurepip_only_when_the_runtime_has_it(plugin_modules, monkeypatch, has_ensurepip):
    """Packaged macOS and Linux runtimes ship without ensurepip; plugin environments install nothing."""
    from row_bot.plugins import sandbox

    installer = plugin_modules["installer"]
    environment = installer._generation_path("sample-plugin", str(uuid4()), create=True)
    find_spec = sandbox.importlib.util.find_spec
    monkeypatch.setattr(sandbox, "_embedded_runtime", lambda: None)
    monkeypatch.setattr(sandbox.importlib.util, "find_spec",
                        lambda name, *args: None if name == "ensurepip" and not has_ensurepip else find_spec(name, *args))
    seen = []

    def create(argv, *, cwd, timeout):
        seen.append(list(argv))
        venv.EnvBuilder(with_pip=False, symlinks=False).create(environment)

    monkeypatch.setattr(sandbox, "_run", create)
    sandbox.create_environment(environment)

    assert seen[0][:5] == [sys.executable, "-I", "-m", "venv", "--copies"]
    assert ("--without-pip" in seen[0]) is not has_ensurepip
    sandbox._target(environment)


@pytest.mark.parametrize("bundled_library", [True, False])
def test_a_copied_macos_interpreter_gets_the_libpython_it_loads_beside_itself(plugin_modules, monkeypatch, tmp_path,
                                                                               bundled_library):
    """The packaged macOS python loads @executable_path/../lib/libpythonX.Y.dylib; a copy without it cannot start."""
    from row_bot.plugins import sandbox

    installer = plugin_modules["installer"]
    name = f"libpython{sys.version_info.major}.{sys.version_info.minor}.dylib"
    runtime = tmp_path / "Row-Bot.app" / "Contents" / "Resources" / "python"
    (runtime / "bin").mkdir(parents=True)
    (runtime / "lib").mkdir()
    (runtime / "bin" / "python3").write_bytes(b"interpreter")
    if bundled_library:
        (runtime / "lib" / name).write_bytes(b"shared library")
    environment = installer._generation_path("sample-plugin", str(uuid4()), create=True)
    monkeypatch.setattr(sandbox, "_embedded_runtime", lambda: None)

    def create(argv, *, cwd, timeout):
        venv.EnvBuilder(with_pip=False, symlinks=False).create(environment)
        # From here on, the packaged macOS app's runtime made it.
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(sys, "executable", str(runtime / "bin" / "python3"))

    monkeypatch.setattr(sandbox, "_run", create)
    sandbox.create_environment(environment)

    copied = environment / "lib" / name
    if bundled_library:
        assert copied.read_bytes() == b"shared library" and not copied.is_symlink()
    else:
        assert not os.path.lexists(copied)
    assert installer._tree_revision(environment, source=False)
    sandbox._target(environment)


@pytest.mark.skipif(os.name != "nt", reason="the private copy is for the Windows embeddable runtime")
def test_the_packaged_windows_runtime_gets_a_private_isolated_copy(plugin_modules, monkeypatch, tmp_path):
    """A venv of the embeddable runtime would read its ._pth and see Row-Bot's own libraries."""
    from row_bot.plugins import sandbox

    installer = plugin_modules["installer"]
    tag = f"{sys.version_info.major}{sys.version_info.minor}"
    runtime = tmp_path / "runtime"
    (runtime / "Lib" / "site-packages" / "hostpkg").mkdir(parents=True)
    interpreter = ["python.exe", "pythonw.exe", f"python{tag}.dll", "_ssl.pyd", f"python{tag}.zip"]
    for name in [*interpreter, "LICENSE.txt"]:
        (runtime / name).write_bytes(name.encode())
    (runtime / f"python{tag}._pth").write_text(f"python{tag}.zip\n.\nimport site\nLib\\site-packages\n..\\app\\src\n")
    monkeypatch.setattr(sys, "executable", str(runtime / "python.exe"))

    def no_process(argv, **_kwargs):
        raise AssertionError(f"the private runtime is copied, not made by {argv}")

    monkeypatch.setattr(sandbox, "_run", no_process)
    environment = installer._generation_path("sample-plugin", str(uuid4()), create=True)
    sandbox.create_environment(environment)

    scripts = environment / "Scripts"
    assert sorted(path.name for path in scripts.iterdir()) == sorted([*interpreter, f"python{tag}._pth"])
    # Only the copy's own standard library and folder: none of Row-Bot's paths.
    assert (scripts / f"python{tag}._pth").read_text().splitlines() == [f"python{tag}.zip", "."]
    config = (environment / "pyvenv.cfg").read_text()
    assert "include-system-site-packages = false" in config and "home" not in config
    assert list((environment / "Lib" / "site-packages").iterdir()) == []
    python, _identity = sandbox._target(environment)
    assert python == scripts / "python.exe"


def test_copied_environment_creation_is_link_free(plugin_modules, monkeypatch):
    from row_bot.plugins import sandbox

    installer = plugin_modules["installer"]
    environment = installer._generation_path("sample-plugin", str(uuid4()), create=True)

    def create(argv, *, cwd, timeout):
        assert argv[-1] == str(environment)
        venv.EnvBuilder(with_pip=False, symlinks=False).create(environment)

    monkeypatch.setattr(sandbox, "_run", create)
    sandbox.create_environment(environment)
    assert not (environment / "lib64").is_symlink()
    assert installer._tree_revision(environment, source=False)
    sandbox._target(environment)


@pytest.mark.skipif(os.name == "nt", reason="POSIX venv alias layout")
@pytest.mark.parametrize("target", ["lib", "../outside", "missing"])
def test_only_standard_alias_is_normalised(plugin_modules, target):
    from row_bot.plugins import sandbox

    installer = plugin_modules["installer"]
    environment = installer._generation_path("sample-plugin", str(uuid4()), create=True)
    environment.mkdir()
    (environment / "lib").mkdir()
    alias = environment / "lib64"
    alias.symlink_to(target)
    if target == "lib":
        sandbox._normalise_created_environment(environment)
        assert not os.path.lexists(alias)
        # Introducing the same alias after preparation is still rejected.
        alias.symlink_to("lib")
        with pytest.raises(sandbox.EnvironmentError, match="environment_path_invalid"):
            installer._tree_revision(environment, source=False)
    else:
        with pytest.raises(sandbox.EnvironmentError, match="environment_path_invalid"):
            sandbox._normalise_created_environment(environment)
        assert alias.is_symlink()
