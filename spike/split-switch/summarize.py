"""Summarize downloaded split-and-switch trial artifacts into Markdown (run locally).

  python summarize.py <dir containing split-switch-<os>/ and split-switch-parts/>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MB = 1_000_000


def mb(value) -> str:  # noqa: ANN001
    return "-" if value in (None, "") else f"{value / MB:,.2f}"


def s(value) -> str:  # noqa: ANN001
    return "-" if value is None else f"{value:,.1f}" if isinstance(value, (int, float)) else str(value)


def load(path: Path):  # noqa: ANN201
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def parts_section(parts: dict) -> None:
    print("## Parts (built once, platform-independent)\n")
    print("| Part | files | uncompressed MB | tar.xz MB | tar.gz MB | zip MB | xz seconds |")
    print("|---|---|---|---|---|---|---|")
    for name in ("code", "assets"):
        p = parts[name]
        f = p["formats"]
        print(f"| {name} | {p['files']:,} | {mb(p['uncompressed_bytes'])} | {mb(f['tar.xz']['bytes'])} | {mb(f['tar.gz']['bytes'])} | {mb(f['zip']['bytes'])} | {f['tar.xz']['seconds']} |")
    print(f"\nUpdate JSON: {parts['update_json_bytes']} bytes. Hashed exports: " + ", ".join(
        f"{v}: requirements {mb(e['requirements_hashed']['bytes'])} MB, pylock {mb(e['pylock']['bytes'])} MB" for v, e in parts["exports"].items()))
    print("\n### Libraries changed 5.0.0 -> 5.1.0 (lock diff)\n")
    for target, info in parts["changes_from_base"].items():
        rows = ", ".join(f"{r['name']} {r['from']}->{r['to']} ({mb(r['wheel_bytes'])} MB)" for r in info["rows"])
        print(f"- {target}: {info['changed']} changed, {mb(info.get('download_bytes_known'))} MB of wheels: {rows}")
    if parts.get("big_jump_from_base"):
        print("\n### Big jump 5.0.0 -> 4.5.0 lock (synthetic large library change)\n")
        for target, info in parts["big_jump_from_base"].items():
            print(f"- {target}: {info['changed']} changed, {mb(info.get('download_bytes_known'))} MB of wheels with sizes in the lock; size not in lock: {info.get('size_unknown_in_lock')}")
    print("\n### Wheel availability (today's lock)\n")
    print("| Target | released | packages | with wheel | needs a source build | needs newer OS than floor |")
    print("|---|---|---|---|---|---|")
    for target, info in parts["wheel_availability"].items():
        src = ", ".join(f"{r['name']}=={r['version']}" for r in info["source_build_needed"]) or "none"
        newer = ", ".join(f"{r['name']} ({r['needs']})" for r in info["needs_newer_than_floor"]) or "none"
        print(f"| {target} | {info['released_platform']} | {info['packages']} | {info['with_wheel']} | {src} | floor {info['floor']}: {newer} |")


def staging_rows(osname: str, sc: dict) -> None:
    print(f"\n### {osname}: staging\n")
    print("| Scenario | ok | parts MB (code+assets) | library MB (net) | hosts | plan changes | clone s | sync s | unpack s | compile s | self-test s | finish s | total s | staged extra MB | 5.0.0 intact | error |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for name, r in sc.items():
        if "timings" not in r or "tool" not in r:
            continue
        t = r["timings"]
        parts = sum(d["bytes"] for d in r.get("downloads") or [])
        net = r.get("network") or {}
        hosts = ", ".join(f"{h} {mb(v['bytes_down'])}" for h, v in (net.get("hosts") or {}).items())
        plan = r.get("plan") or {}
        changes = f"{len(plan.get('change', []))}c/{len(plan.get('add', []))}a/{len(plan.get('remove', []))}r" if plan else "-"
        integ = r.get("base_integrity")
        intact = "-" if not integ else ("yes" if integ.get("ok") else f"NO ({integ['python']['content_changed']}+{integ['python']['removed']})")
        staged = (r.get("staged_size") or {}).get("bytes_not_shared_by_inode")
        err = (r.get("error") or {}).get("step", "")
        if err:
            err += ": " + (r["error"].get("message") or "")[:90].replace("|", "/").replace("\n", " ")
        print(
            f"| {name} | {r.get('ok')} | {mb(parts)} | {mb(net.get('bytes_down'))} | {hosts or '-'} | {changes} | {s(t.get('clone'))} | {s(t.get('sync'))} "
            f"| {s(t.get('unpack'))} | {s(t.get('compile'))} | {s(t.get('selftest'))} | {s(t.get('finish'))} | {s(t.get('total'))} | {mb(staged)} | {intact} | {err} |"
        )


def switch_rows(osname: str, sc: dict) -> None:
    print(f"\n### {osname}: switch and relaunch\n")
    print("| Scenario | direction | signal | app exit s | exit -> new server ready s | request -> ready s | verified after ready s | port owner in new version | old processes left | launcher graceful |")
    print("|---|---|---|---|---|---|---|---|---|---|")
    for name, r in sc.items():
        for key in ("switch", "switch_back"):
            sw = r.get(key) if key in r else (r if name == "final_switch" and key == "switch" else None)
            if not isinstance(sw, dict) or "seconds" not in sw:
                continue
            sec = sw["seconds"]
            print(
                f"| {name} | {sw['from']} -> {sw['to']} | {sw['signal'][:18]} | {s(sec['app_exit'])} | {s(sec['exit_to_new_server_ready'])} | {s(sec['request_to_new_server_ready'])} "
                f"| {s(sec['verified_after_ready'])} | {sw['port_owner_in_new_version']} | {len(sw['old_version_processes_left'])} | {sw['launcher_exited_gracefully']} |"
            )


def clone_rows(osname: str, sc: dict) -> None:
    print(f"\n### {osname}: clone python/ only\n")
    print("| Scenario | method | files | seconds | disk free delta MB | extra MB by inode | rename while running | delete s | delete failures |")
    print("|---|---|---|---|---|---|---|---|---|")
    for name, r in sc.items():
        if not name.startswith(("clone_", "defender_clone_")) or "method" not in r:
            continue
        print(
            f"| {name} | {r["method"]}{(":" + str(r["threads"])) if r.get("threads") else ""} | {r["size"]["files"]:,} | {r['seconds']} | {mb(r['disk_free_delta_bytes'])} | {mb(r['size']['bytes_not_shared_by_inode'])} "
            f"| {r['rename'].get('ok')} | {r['delete']['seconds']} | {r['delete']['failure_count']} |"
        )


def main() -> None:
    root = Path(sys.argv[1])
    parts = load(root / "split-switch-parts" / "parts-report.json")
    if parts:
        parts_section(parts)
    for folder in sorted(root.glob("split-switch-*")):
        res = load(folder / "results.json")
        if not res:
            continue
        osname = res["os"]
        sc = res["scenarios"]
        print(f"\n## {osname}\n")
        base = sc.get("install_base", {})
        if base.get("python"):
            print(f"- 5.0.0 install: python/ {base['python']['files']:,} files {mb(base['python']['bytes'])} MB; app/ {base['app']['files']:,} files {mb(base['app']['bytes'])} MB; hashing took {base.get('hash_seconds')} s")
            print(f"- assets unchanged 5.0.0 -> 5.1.0: {base.get('assets_content_sha256') == base.get('manifest_assets_content_sha256')}")
        start = sc.get("start_base", {})
        print(f"- first start of 5.0.0: ready in {start.get('ready_seconds')} s; native modules mapped: {start.get('loaded_native')}")
        clone_rows(osname, sc)
        staging_rows(osname, sc)
        switch_rows(osname, sc)
        for name in ("defender_on", "defender_status_end", "rollback_rule", "cleanup_old_versions", "linux_launch_mode", "linux_systemd", "mac_seed_checks"):
            if name in sc:
                print(f"\n#### {osname}: {name}\n\n```json\n{json.dumps(sc[name], indent=1, default=str)[:5000]}\n```")


if __name__ == "__main__":
    main()
