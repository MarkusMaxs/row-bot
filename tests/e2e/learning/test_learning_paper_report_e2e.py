"""Opt-in: the paper's report on copies of S1's seeded people, for real (P1).

Runs ``scripts/learning_paper_report.py`` as the participant would, ``--health`` and then ``--freeze``, on a
copy of each seeded person's profile (never S1's own folders). The freeze serves the person's personal model
and its starting model in Ollama on this computer's graphics card (no cloud tokens), so it is opt-in:

    $env:ROW_BOT_LEARNING_PAPER_E2E = "1"
    $env:ROW_BOT_LEARNING_PAPER_PROFILES = "<folder with <person>\\data copies of S1's seeded profiles>"
    $env:ROW_BOT_LEARNING_PAPER_SCRATCH = "<a scratch folder>"

Each JSON passes the report's schema, holds none of the person's names, conversation ids or folders, and the
freeze's arms agree with S1's own end-of-run benchmark (``versions/v1/comparison.json`` in the profile, run by
the product on the same hidden problems) within the run-to-run noise: each arm's pass rate on the hidden packs
and the held-out work within 1.96 x sqrt(2) standard errors of S1's, and the two paired intervals overlap.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.e2e]

ROOT = Path(__file__).resolve().parents[3]
ENABLED = os.environ.get("ROW_BOT_LEARNING_PAPER_E2E") == "1"
PEOPLE = {"idris": ("Idris", "Fenwick"), "mara": ("Mara", "Quillon"), "wren": ("Wren", "Achterberg")}


def _folders(person: str) -> tuple[Path, Path, Path]:
    profiles = os.environ.get("ROW_BOT_LEARNING_PAPER_PROFILES", "")
    scratch = os.environ.get("ROW_BOT_LEARNING_PAPER_SCRATCH", "")
    if not profiles or not scratch:
        pytest.skip("set ROW_BOT_LEARNING_PAPER_PROFILES and ROW_BOT_LEARNING_PAPER_SCRATCH")
    profile = Path(profiles) / person / "data"
    if not (profile / "learning.db").is_file():
        pytest.skip(f"no copy of {person}'s seeded profile")
    return profile, Path(scratch) / "copies", Path(scratch) / "out" / person


def _run(mode: list[str], profile: Path, scratch: Path, out: Path) -> tuple[dict, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("ROW_BOT_")}
    before = set(out.glob("*.json")) if out.is_dir() else set()
    finished = subprocess.run([sys.executable, str(ROOT / "scripts" / "learning_paper_report.py"), *mode,
                               "--profile", str(profile), "--scratch", str(scratch), "--out", str(out), "--yes"],
                              cwd=ROOT, env=env, capture_output=True, text=True, timeout=4 * 3600)
    assert finished.returncode == 0, finished.stdout[-4000:] + finished.stderr[-4000:]
    (written,) = set(out.glob("*.json")) - before
    return json.loads(written.read_text(encoding="utf-8")), finished.stdout + finished.stderr


def _private(text: str, person: str, profile: Path, scratch: Path) -> None:
    for word in (*PEOPLE[person], f"s1-{person}", str(profile), profile.as_posix(), str(scratch), "rel_", "job_",
                 "dec_", "row-bot-personal"):
        assert word not in text


def _agrees(mine: float, theirs: float, problems: int) -> bool:
    share = min(max((mine + theirs) / 2, 0.05), 0.95)
    return abs(mine - theirs) <= 1.96 * math.sqrt(2) * math.sqrt(share * (1 - share) / max(problems, 1))


def cross_check(freeze: dict, comparison: dict) -> dict:
    """The freeze's start and personal arms beside S1's benchmark of the same version (hidden part)."""

    analysis = (comparison.get("analysis") or {}).get("hidden") or {}
    version = f"v{freeze['versions']['evaluated']}"
    paired = (analysis.get(version) or {}).get("by_kind") or {}
    own = paired.get("own_work") or {}
    packs = [paired[key] for key in ("document_lookups", "structured_transforms", "small_coding", "tool_workflows")
             if key in paired]
    s1_packs = {"baseline": sum(item["baseline"] * item["problems"] for item in packs) / sum(item["problems"] for item in packs),
                "arm": sum(item["arm"] * item["problems"] for item in packs) / sum(item["problems"] for item in packs),
                "problems": sum(item["problems"] for item in packs)}
    mine = freeze["arms"]
    contrast = freeze["contrasts"]["personal vs start"]
    rows = {}
    for split, theirs in (("real_uncorrected", own), ("packs", s1_packs)):
        problems = int(theirs["problems"])
        rows[split] = {
            "start": (mine["start"]["splits"][split]["mean"], theirs["baseline"]),
            "personal": (mine["personal"]["splits"][split]["mean"], theirs["arm"]),
            "problems": (mine["start"]["splits"][split]["counted"], problems),
            "agree": all(_agrees(float(a), float(b), problems) for a, b in
                         ((mine["start"]["splits"][split]["mean"], theirs["baseline"]),
                          (mine["personal"]["splits"][split]["mean"], theirs["arm"]))),
        }
    primary = contrast["real_uncorrected"]["primary"]
    rows["own_work_difference"] = {"p1": (primary["difference"], primary["low"], primary["high"]),
                                   "s1": (own.get("difference"), own.get("low"), own.get("high")),
                                   "overlap": primary["low"] <= own["high"] and own["low"] <= primary["high"]}
    return rows


@pytest.mark.skipif(not ENABLED, reason="opt-in: set ROW_BOT_LEARNING_PAPER_E2E=1")
@pytest.mark.parametrize("person", sorted(PEOPLE))
def test_health_and_the_freeze_on_a_seeded_person_agree_with_s1(person):
    sys.path.insert(0, str(ROOT))
    from scripts import learning_paper_report as report

    profile, scratch, out = _folders(person)
    comparison = json.loads((profile / "learning_models" / "versions" / "v1" / "comparison.json")
                            .read_text(encoding="utf-8"))

    health, printed = _run(["--health"], profile, scratch, out)
    report.validate(health, report.HEALTH)
    _private(printed + json.dumps(health), person, profile, scratch)
    assert health["hidden_problems"]["uncorrected"] >= 20 and health["versions"]["ready"] >= 1

    freeze, printed = _run(["--freeze", "--as-of", health["recording"]["last_day"]], profile, scratch, out)
    report.validate(freeze, report.FREEZE)
    _private(printed + json.dumps(freeze), person, profile, scratch)
    assert freeze["versions"]["evaluated"] == 1 and freeze["design"]["container"]
    found = cross_check(freeze, comparison)
    (scratch.parent / f"p1_crosscheck_{person}.json").write_text(json.dumps(found, indent=1), encoding="utf-8")
    assert found["real_uncorrected"]["agree"] and found["packs"]["agree"], found
    assert found["own_work_difference"]["overlap"], found
    assert list(scratch.iterdir()) == []  # every copy deleted
