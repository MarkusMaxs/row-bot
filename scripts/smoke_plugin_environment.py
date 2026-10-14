"""Smoke: a packaged runtime can create an isolated plugin environment.

Run with an installed Row-Bot's own interpreter, for example::

    <version>/python/python.exe -I -B scripts/smoke_plugin_environment.py --app <version>/app

It creates a worker-plugin environment the way Row-Bot does
(``row_bot.plugins.sandbox.create_environment``) in a throwaway data folder,
then starts that environment's interpreter the way a plugin worker does
(``-I -S -B`` plus its own site-packages) and checks the standard library
works while Row-Bot's own code and libraries cannot be imported.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

_CHECK = r"""
import importlib.util, json, os, sys
environment = sys.argv[1]
site = (os.path.join(environment, "Lib", "site-packages") if os.name == "nt" else
        os.path.join(environment, "lib", f"python{sys.version_info.major}.{sys.version_info.minor}", "site-packages"))
sys.path.append(site)
stdlib = {}
for name in ("ssl", "sqlite3", "ctypes", "json", "asyncio", "hashlib", "zipfile"):
    try:
        __import__(name)
        stdlib[name] = True
    except Exception as exc:
        stdlib[name] = f"{type(exc).__name__}: {exc}"
host = {name: importlib.util.find_spec(name) is not None for name in ("row_bot", "fastapi", "pydantic", "packaging")}
print(json.dumps({"version": sys.version.split()[0], "stdlib": stdlib, "host_importable": host}))
"""

_HOST_MODULES = ("row_bot", "fastapi", "pydantic", "packaging")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app", type=Path, required=True, help="The installed version's app/ folder")
    args = parser.parse_args(argv)

    # Resolved: the sandbox refuses data folders reached through a link (macOS /var).
    data = Path(tempfile.mkdtemp(prefix="rb-plugin-env-")).resolve()
    try:
        os.environ["ROW_BOT_DATA_DIR"] = str(data)
        sys.path.insert(0, str(args.app.resolve() / "src"))
        from row_bot.plugins import installer, sandbox

        environment = installer._generation_path("smoke-plugin", str(uuid4()), create=True)
        sandbox.create_environment(environment)
        python, _ = sandbox._target(environment)
        run = subprocess.run([str(python), "-I", "-S", "-B", "-c", _CHECK, str(environment)],
                             capture_output=True, text=True, timeout=120, check=False)
        if run.returncode != 0:
            print(run.stdout, run.stderr, sep="\n", file=sys.stderr)
            print("FAIL: the plugin environment's interpreter did not start")
            return 1
        report = json.loads(run.stdout.strip().splitlines()[-1])
        report["environment_mb"] = round(sum(p.stat().st_size for p in environment.rglob("*") if p.is_file()) / 1e6, 1)
        print(json.dumps(report, indent=2))
        broken = [name for name, ok in report["stdlib"].items() if ok is not True]
        leaked = [name for name in _HOST_MODULES if report["host_importable"].get(name)]
        if broken:
            print(f"FAIL: standard library modules failed in the plugin environment: {broken}")
            return 1
        if leaked:
            print(f"FAIL: the plugin environment can import Row-Bot's own code or libraries: {leaked}")
            return 1
        print("OK: plugin environment created, its standard library works and Row-Bot's code is not reachable")
        return 0
    finally:
        shutil.rmtree(data, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
