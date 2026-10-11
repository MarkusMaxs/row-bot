"""Wheel availability per target platform for a pylock.toml export (Phase 1 spike).

For every package that applies to a target (marker evaluated for that target),
check whether the lock lists a wheel the target can install. Packages with
only an sdist would need a source build, which the update engine forbids.
Also reports the newest macOS / glibc version any chosen wheel needs.

  python wheelcheck.py --pylock pylock.toml --out wheels.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote

from packaging import tags
from packaging.markers import Marker
from packaging.utils import parse_wheel_filename

MAC_VERSIONS = [(11, 0), (12, 0), (13, 0), (14, 0), (15, 0), (26, 0)]
GLIBC_VERSIONS = [(2, 17), (2, 28), (2, 31), (2, 34), (2, 35), (2, 39)]


def _env(sys_platform: str, machine: str, system: str, os_name: str, full: str) -> dict:
    major_minor = ".".join(full.split(".")[:2])
    return {
        "sys_platform": sys_platform,
        "platform_machine": machine,
        "platform_system": system,
        "os_name": os_name,
        "python_version": major_minor,
        "python_full_version": full,
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "implementation_version": full,
        "platform_release": "",
        "platform_version": "",
        "extra": "",
    }


def _cp(py: tuple[int, int], platforms: list[str]) -> list[tags.Tag]:
    abi = f"cp{py[0]}{py[1]}"
    return list(tags.cpython_tags(py, abis=[abi], platforms=platforms)) + list(
        tags.compatible_tags(py, f"cp{py[0]}{py[1]}", platforms)
    )


def _manylinux(arch: str, glibc: tuple[int, int]) -> list[str]:
    plats = [f"manylinux_2_{minor}_{arch}" for minor in range(glibc[1], 4, -1)]
    legacy = {17: "manylinux2014", 12: "manylinux2010", 5: "manylinux1"}
    for minor, name in legacy.items():
        if minor <= glibc[1] and (arch in {"x86_64", "i686"} or name == "manylinux2014"):
            plats.append(f"{name}_{arch}")
    return plats


TARGETS = {
    "win_amd64-cp313": {
        "env": _env("win32", "AMD64", "Windows", "nt", "3.13.2"),
        "tags": lambda _v: _cp((3, 13), ["win_amd64"]),
        "versions": [None],
        "released": True,
    },
    "macos_arm64-cp312": {
        "env": _env("darwin", "arm64", "Darwin", "posix", "3.12.13"),
        "tags": lambda v: _cp((3, 12), list(tags.mac_platforms(v, "arm64"))),
        "versions": MAC_VERSIONS,
        "released": True,
    },
    "macos_x86_64-cp312": {
        "env": _env("darwin", "x86_64", "Darwin", "posix", "3.12.13"),
        "tags": lambda v: _cp((3, 12), list(tags.mac_platforms(v, "x86_64"))),
        "versions": MAC_VERSIONS,
        "released": False,
    },
    "manylinux_x86_64-cp312": {
        "env": _env("linux", "x86_64", "Linux", "posix", "3.12.13"),
        "tags": lambda v: _cp((3, 12), _manylinux("x86_64", v)),
        "versions": GLIBC_VERSIONS,
        "released": True,
    },
    "manylinux_aarch64-cp312": {
        "env": _env("linux", "aarch64", "Linux", "posix", "3.12.13"),
        "tags": lambda v: _cp((3, 12), _manylinux("aarch64", v)),
        "versions": GLIBC_VERSIONS,
        "released": False,
    },
}


def wheel_tags(url: str) -> frozenset[tags.Tag]:
    name = unquote(url.rsplit("/", 1)[-1].split("#", 1)[0])
    return parse_wheel_filename(name)[3]


def check(pylock: Path) -> dict:
    data = tomllib.loads(pylock.read_text(encoding="utf-8"))
    packages = data["packages"]
    result: dict = {"pylock": str(pylock), "package_entries": len(packages), "targets": {}}
    for target, spec in TARGETS.items():
        env = spec["env"]
        tag_sets = {v: set(spec["tags"](v)) for v in spec["versions"]}
        rows = []
        for pkg in packages:
            marker = pkg.get("marker")
            if marker and not Marker(marker).evaluate(env):
                continue
            wheels = pkg.get("wheels") or []
            needs = None
            chosen = None
            for version in spec["versions"]:
                supported = tag_sets[version]
                match = [w for w in wheels if wheel_tags(w["url"]) & supported]
                if match:
                    needs = version
                    chosen = max(match, key=lambda w: w.get("size", 0))
                    break
            rows.append(
                {
                    "name": pkg["name"],
                    "version": pkg["version"],
                    "has_sdist": bool(pkg.get("sdist")),
                    "wheel_count": len(wheels),
                    "needs": ".".join(map(str, needs)) if needs else ("any" if chosen else None),
                    "wheel": unquote(chosen["url"].rsplit("/", 1)[-1]) if chosen else None,
                    "wheel_size": chosen.get("size") if chosen else None,
                    "index": pkg.get("index"),
                }
            )
        missing = [r for r in rows if r["wheel"] is None]
        floor = spec["versions"][0]
        above_floor = [r for r in rows if r["wheel"] and r["needs"] not in ("any", None) and floor and r["needs"] != ".".join(map(str, floor))]
        result["targets"][target] = {
            "released_platform": spec["released"],
            "packages": len(rows),
            "with_wheel": len(rows) - len(missing),
            "source_build_needed": [
                {"name": r["name"], "version": r["version"], "has_sdist": r["has_sdist"], "any_wheels_for_other_platforms": r["wheel_count"]}
                for r in missing
            ],
            "needs_newer_than_floor": [
                {"name": r["name"], "version": r["version"], "needs": r["needs"], "wheel": r["wheel"]} for r in above_floor
            ],
            "floor": ".".join(map(str, floor)) if floor else None,
            "total_wheel_bytes": sum(r["wheel_size"] or 0 for r in rows),
            "rows": rows,
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pylock", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = check(args.pylock)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for target, info in result["targets"].items():
        names = ", ".join(f"{r['name']}=={r['version']}" for r in info["source_build_needed"]) or "none"
        newer = ", ".join(f"{r['name']} ({r['needs']})" for r in info["needs_newer_than_floor"]) or "none"
        print(
            f"{target}: {info['with_wheel']}/{info['packages']} have wheels; "
            f"total {info['total_wheel_bytes'] / 1e6:.0f} MB; source build needed: {names}; "
            f"newer than {info['floor']}: {newer}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
