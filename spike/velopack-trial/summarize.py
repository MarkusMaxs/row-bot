"""Summarize downloaded trial artifacts into Markdown tables (run locally).

  python summarize.py <dir containing velopack-trial-<os>-<variant>/ folders>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MB = 1_000_000


def mb(value: float | int | None) -> str:
    return "-" if value is None else f"{value / MB:,.1f}"


def load(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def timings(folder: Path) -> dict:
    rows = {}
    path = folder / "timings.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            rows[row["label"]] = row["seconds"]
    return rows


def main() -> None:
    root = Path(sys.argv[1])
    print("## Sizes (MB = 10^6 bytes)\n")
    print("| OS | variant | payload files 5.1 | payload MB 5.1 | full 5.0 | full 5.1 | installer 5.1 | delta BestSpeed | delta BestSize | delta (fresh sigs) | delta zip overhead | delta entries |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for folder in sorted(root.glob("velopack-trial-*")):
        sizes = load(folder / "sizes.json")
        if not sizes:
            continue
        payload = (sizes.get("payloads") or [{}, {}])[-1]
        extra = sizes.get("extra_deltas", {})
        installers = list(sizes.get("installer", {}).values())
        print(
            f"| {sizes['os']} | {sizes['variant']} | {payload.get('file_count', '-'):,} | {mb(payload.get('bytes'))} "
            f"| {mb(sizes['full'].get('5.0.0'))} | {mb(sizes['full'].get('5.1.0'))} | {mb(installers[-1] if installers else None)} "
            f"| {mb(sizes['delta']['bytes'])} | {mb(extra.get('bestsize', {}).get('bytes'))} "
            f"| {mb(extra.get('fresh_signatures', {}).get('bytes'))} | {mb(sizes['delta']['zip_overhead_bytes'])} "
            f"| {sizes['delta']['entries']:,} |"
        )
    for folder in sorted(root.glob("velopack-trial-*")):
        sizes = load(folder / "sizes.json")
        if not sizes:
            continue
        print(f"\n### {folder.name}: delta composition (compressed MB)\n")
        for label, delta in [("BestSpeed", sizes["delta"]), *sizes.get("extra_deltas", {}).items()]:
            kinds = ", ".join(f"{k} {v['count']:,} / {mb(v['bytes'])}" for k, v in delta["by_kind"].items())
            cats = ", ".join(
                f"{k} {v['count']:,} / {mb(v['bytes'])}" for k, v in sorted(delta["changed_or_new_by_category"].items())
            )
            print(f"- **{label}** {mb(delta['bytes'])} MB. Kinds: {kinds}. Changed/new by category: {cats}")
        diff = sizes["nupkg_diff"]["table"]
        print("- nupkg diff 5.0->5.1: " + ", ".join(f"{k} {v['count']:,}" for k, v in diff.items()))
        for label, extra in (sizes.get("extra_diffs") or {}).items():
            print(f"- nupkg diff ({label}): " + ", ".join(f"{k} {v['count']:,}" for k, v in extra["table"].items()))
        t = timings(folder)
        if t:
            print("- timings (s): " + ", ".join(f"{k} {v}" for k, v in t.items()))
        for payload in sizes.get("payloads", []):
            print(
                f"- payload {payload['version']}: {payload['file_count']:,} files, {mb(payload['bytes'])} MB, "
                f"build timings {payload.get('timings')}, compile {payload.get('compile')}"
            )
        for sign in sorted(folder.glob("sign-*.json")):
            data = load(sign) or {}
            print(
                f"- {sign.stem}: libraries {data.get('libraries')}, signed {data.get('signed')}, "
                f"reused {data.get('reused')}, {data.get('seconds')} s"
            )
        notary = load(folder / "notary-time.json")
        if notary:
            print(f"- notarization: {notary}; submit result: {load(folder / 'notary-submit.json')}")

    print("\n## Scenarios\n")
    print("| OS | scenario | install s | check s | download+stage s | apply+relaunch s | installed after | fell back | tree ok | signature | children alive after | peak extra disk MB | error |")
    print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for folder in sorted(root.glob("velopack-trial-*")):
        data = load(folder / "scenarios" / "scenarios.json")
        if not data:
            continue
        for name, rec in data["scenarios"].items():
            if name == "uninstall":
                print(f"| {data['os']} | uninstall | {rec} |")
                continue
            inst = rec.get("install", {})
            upd = rec.get("update") or rec.get("probe") or {}
            install_s = inst.get("setup_seconds", inst.get("unzip_seconds"))
            checked = (upd.get("checked") or {}).get("seconds")
            dl = upd.get("download_outcome") or {}
            restarted = upd.get("restarted") or {}
            tree = rec.get("tree") or {}
            tree_ok = (
                "-" if not tree else ("yes" if not (tree["missing"] or tree["mismatched"]) else f"no ({tree['missing']} missing, {tree['mismatched']} mismatched)")
            )
            sig = rec.get("signature") or {}
            sig_s = "-" if not sig else f"verify {sig.get('codesign_verify_rc')}, spctl {sig.get('spctl_rc')}, team {sig.get('team_id')}"
            alive = upd.get("children_alive_after")
            print(
                f"| {data['os']} | {name} | {install_s} | {round(checked, 2) if checked else '-'} "
                f"| {round(dl.get('seconds'), 1) if dl.get('seconds') else dl.get('event', '-')} "
                f"| {rec.get('apply_relaunch_seconds', '-')} | {restarted.get('installed_version', '-')} "
                f"| {rec.get('fell_back_to_full', '-')} | {tree_ok} | {sig_s} | {alive} "
                f"| {mb((upd.get('disk') or {}).get('peak_extra_bytes'))} | {rec.get('error', '')} |"
            )


if __name__ == "__main__":
    main()
