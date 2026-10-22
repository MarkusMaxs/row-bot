"""Row-Bot Learning: the paper's report, run by the participant on their own computer (P1, O83, O109).

The paper's real-data numbers come only from this script (pre-registration §3.4, §7, §10). It writes one
JSON file of counts, rates, intervals and p-values; per-problem results never leave Row-Bot's evaluation
module, and nothing from conversations, requests, replies, tasks, lessons, memories or tool arguments, no
id, path, file name, contact, account or app name ever appears in what it prints or writes (a strict
schema: fixed keys, numbers, dates at day resolution, versions, build commits and hashes; anything else
stops the run).

Modes (PowerShell, from the Row-Bot checkout the Learning chat names, after ``uv sync``)::

    uv run python scripts/learning_paper_report.py --health --profile $HOME\\.row-bot --scratch <folder> --out <folder>
    uv run python scripts/learning_paper_report.py --freeze --as-of 2026-11-02 --profile ... --scratch ... --out ...
    uv run python scripts/learning_paper_report.py --thinking --profile ... --scratch ... --out ...

- ``--health`` (any time between freezes, §10): counts and errors only, never a pass rate or a comparison.
- ``--freeze --as-of <date>`` (§7): the pre-registered freeze evaluation on every hidden problem up to the
  end of that day: H1 (the newest personal-model version that isn't a pilot vs its untouched starting
  model), H2 (or why it can't be tested yet), Holm across {H1, H2}, the minimum size, §7's sensitivity
  analyses. Needs the graphics card and Ollama; about 15-60 minutes.
- ``--thinking``: O97's recheck, ``scripts/learning_shape_ablation.py --thinking both`` on the copy, its
  results reduced to the same kind of aggregates. About 3 hours of the graphics card.

Every mode:

1. prints the SHA-256 of this script and of the product code it calls (log them in the Preview log);
2. refuses while Row-Bot runs on the profile (its launcher lock; for the everyday profile also the
   installed app), never opens the profile for writing, copies what the mode reads to a new folder in
   ``--scratch``, checks the copy (every file, the same bytes), and runs only on the copy;
3. prints exactly what it will write and asks (``--yes`` skips the question), then writes one JSON file to
   ``--out``;
4. deletes the copy (``--keep-copy`` keeps it) and says so.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FORMAT = "row_bot.learning.paper_report"
SCHEMA_VERSION = 1
MODES = ("health", "freeze", "thinking")
COPY_PREFIX = "row-bot-paper-copy-"
#: The pre-registered confirmatory family of paper v1 (§7) and the decision rules.
FAMILY = ("H1", "H2")
ALPHA = 0.05
MARGIN = 0.10
#: What the freeze may spend on cloud models (the main model's reference, H2's arms), at most.
CLOUD_CAP = 5_000_000
#: The product code whose SHA-256 is printed before every run (with this script).
CODE_DIRS = ("src/row_bot/learning",)
CODE_FILES = ("scripts/learning_shape_ablation.py",)
#: What ``--health`` reads, so it copies only these (paths relative to the profile, POSIX style).
HEALTH_FILES = re.compile(r"^(learning\.db(-wal|-shm)?|usage\.db(-wal|-shm)?|learning_config\.json|providers\.json"
                          r"|learning/.*|learning_models/.*\.json)$")
INSTALL_DIR = ("Programs", "Row-Bot")


class Refused(SystemExit):
    """The run stops before touching anything, with a message that names no path."""

    def __init__(self, message: str) -> None:
        super().__init__(2)
        self.message = message


def say(text: str = "") -> None:
    print(text, flush=True)


# ── 1. The code's own hash ──────────────────────────────────────────────────


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def code_hashes(root: Path = ROOT) -> dict[str, Any]:
    """This script's SHA-256 and one SHA-256 over the product files it calls (each file's relative path
    and SHA-256, sorted), the build commit and whether those files differ from it."""

    files: list[Path] = []
    for folder in CODE_DIRS:
        files += [path for path in (root / folder).rglob("*.py") if "__pycache__" not in path.parts]
    files += [root / name for name in CODE_FILES]
    lines = sorted(f"{path.relative_to(root).as_posix()} {_sha(path)}" for path in files if path.is_file())
    product = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    build, clean = None, False
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        found = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=20)
        if found.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", found.stdout.strip()):
            build = found.stdout.strip()
            changed = subprocess.run(["git", "status", "--porcelain", "--", "scripts", *CODE_DIRS], cwd=root,
                                     capture_output=True, text=True, timeout=20)
            clean = changed.returncode == 0 and not changed.stdout.strip()
    return {"script_sha256": _sha(Path(__file__).resolve()), "code_sha256": product, "code_files": len(lines),
            "build": build, "build_clean": clean}


# ── 2. Safety: Row-Bot closed, a checked copy, nothing written to the profile ──


def everyday_profile() -> Path:
    return (Path.home() / ".row-bot").resolve()


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def profile_in_use(profile: Path) -> bool:
    """Whether a Row-Bot launcher holds the profile's lock (the start script's check). The lock file is
    opened read-only, never created."""

    path = profile / "launcher.lock"
    if not path.is_file():
        return False
    try:
        handle = path.open("rb")
    except OSError:
        return True
    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        handle.close()


def installed_app_running() -> bool:
    """Whether the installed Row-Bot app runs (Windows; it opens the everyday profile)."""

    if os.name != "nt" or not os.environ.get("LOCALAPPDATA"):
        return False
    import psutil

    folder = str(Path(os.environ["LOCALAPPDATA"], *INSTALL_DIR)).lower() + os.sep
    for process in psutil.process_iter(["exe"]):
        exe = str(process.info.get("exe") or "").lower()
        if exe.startswith(folder):
            return True
    return False


def check_folders(profile: Path, scratch: Path, out: Path) -> None:
    if not profile.is_dir() or not (profile / "learning.db").is_file():
        raise Refused("The --profile folder isn't a Row-Bot profile with Learning data (no learning.db).")
    for name, folder in (("--scratch", scratch), ("--out", out)):
        if _inside(folder, profile) or _inside(profile, folder):
            raise Refused(f"The {name} folder must be outside the profile (and not contain it).")
    if _inside(scratch, ROOT):
        raise Refused("The --scratch folder must be outside the Row-Bot checkout (a copy there could be committed).")


def refuse_if_running(profile: Path, *, installed: Callable[[], bool] = installed_app_running) -> None:
    if profile_in_use(profile):
        raise Refused("Row-Bot is running on this profile. Quit it (right-click its icon by the clock, then Quit) "
                      "and run this again. Nothing was copied or written.")
    if profile.resolve() == everyday_profile() and installed():
        raise Refused("The installed Row-Bot app is running. Quit it and run this again. Nothing was copied or "
                      "written.")


@dataclass
class Tree:
    files: dict[str, int] = field(default_factory=dict)
    links: int = 0

    @property
    def bytes(self) -> int:
        return sum(self.files.values())


def _is_link(entry: os.DirEntry[str]) -> bool:
    if entry.is_symlink():
        return True
    return bool(getattr(os.path, "isjunction", lambda _path: False)(entry.path))


def measure(root: Path, select: Callable[[str], bool] | None = None) -> Tree:
    """Every regular file under ``root`` (relative POSIX path → size); links and junctions are counted,
    never followed or copied (a link could lead back into the profile)."""

    tree = Tree()
    pending = [root]
    while pending:
        folder = pending.pop()
        with os.scandir(folder) as entries:
            for entry in entries:
                if _is_link(entry):
                    tree.links += 1
                elif entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    relative = Path(entry.path).relative_to(root).as_posix()
                    if select is None or select(relative):
                        tree.files[relative] = entry.stat(follow_symlinks=False).st_size
    return tree


def copy_profile(profile: Path, target: Path, select: Callable[[str], bool] | None = None) -> Tree:
    """Copy the profile's files (reading only) to ``target`` and check the copy: the same files, each the
    same size, and Row-Bot still not running on the profile afterwards."""

    source = measure(profile, select)
    existing = next(folder for folder in (target, *target.parents) if folder.exists())
    if shutil.disk_usage(existing).free < source.bytes + (1 << 30):
        raise Refused("There isn't room for the copy in the --scratch folder (it needs the profile's size plus "
                      "1 GB). Nothing was copied.")
    target.mkdir(parents=True)
    for relative in sorted(source.files):
        destination = target / PurePosixPath(relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with (profile / PurePosixPath(relative)).open("rb") as reading, destination.open("xb") as writing:
            shutil.copyfileobj(reading, writing, 1 << 22)
    copied = measure(target)
    if copied.files != source.files:
        raise CopyMismatch(len(copied.files), copied.bytes, len(source.files), source.bytes)
    if profile_in_use(profile):
        raise CopyMismatch(len(copied.files), copied.bytes, len(source.files), source.bytes, started=True)
    copied.links = source.links
    return copied


class CopyMismatch(Exception):
    def __init__(self, files: int, size: int, want_files: int, want_size: int, *, started: bool = False) -> None:
        self.text = ("Row-Bot started while the profile was copied" if started else
                     f"The copy doesn't match the profile: {files} files, {size} bytes copied of {want_files} "
                     f"and {want_size}")
        super().__init__(self.text)


def remove_copy(target: Path) -> bool:
    def clear(function: Any, path: str, _error: Any) -> None:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)
            function(path)

    for attempt in range(5):
        shutil.rmtree(target, onexc=clear) if sys.version_info >= (3, 12) else shutil.rmtree(target, onerror=clear)
        if not target.exists():
            return True
        time.sleep(1.0 + attempt)
    return not target.exists()


# ── 3. The report's schema: fixed keys, no free text ────────────────────────


class SchemaError(ValueError):
    """The report holds something the schema doesn't allow: the run writes nothing."""


Spec = Any
DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MONTH = re.compile(r"^\d{4}-\d{2}$")
COMMIT = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MODEL_ORDINAL = re.compile(r"^model_\d{1,2}$")


@dataclass(frozen=True)
class Num:
    """A number: ``integer`` (not a bool), within [low, high] when given."""

    integer: bool = False
    low: float | None = None
    high: float | None = None


@dataclass(frozen=True)
class Text:
    """A string from a fixed set, or matching a fixed pattern (dates, commits, hashes)."""

    values: frozenset[str] = frozenset()
    pattern: re.Pattern[str] | None = None


@dataclass(frozen=True)
class Obj:
    fields: Mapping[str, Spec]
    optional: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Map:
    """A mapping whose keys come from a fixed set (or a fixed pattern) and whose values follow ``value``."""

    keys: Text
    value: Spec


@dataclass(frozen=True)
class Seq:
    item: Spec
    most: int = 1000


@dataclass(frozen=True)
class Maybe:
    spec: Spec


BOOL = "bool"
COUNT = Num(integer=True, low=0)
INT = Num(integer=True)
SHARE = Num(low=0, high=1)
SIGNED = Num(low=-1, high=1)
HOURS = Num(low=0)


def enum(*values: str) -> Text:
    return Text(frozenset(values))


def validate(value: Any, spec: Spec, where: str = "report") -> None:
    """Raise :class:`SchemaError` unless ``value`` is exactly what ``spec`` allows. The message names where,
    never the offending key or value (they could be anything)."""

    if isinstance(spec, Maybe):
        if value is not None:
            validate(value, spec.spec, where)
        return
    if spec == BOOL:
        if not isinstance(value, bool):
            raise SchemaError(f"{where}: not a yes/no")
        return
    if isinstance(spec, Num):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or (spec.integer and not isinstance(value, int)):
            raise SchemaError(f"{where}: not a number of the right kind")
        if isinstance(value, float) and not math.isfinite(value):
            raise SchemaError(f"{where}: not a finite number")
        if (spec.low is not None and value < spec.low) or (spec.high is not None and value > spec.high):
            raise SchemaError(f"{where}: out of range")
        return
    if isinstance(spec, Text):
        if not isinstance(value, str) or not _text_ok(value, spec):
            raise SchemaError(f"{where}: text that isn't allowed")
        return
    if isinstance(spec, Obj):
        if not isinstance(value, dict):
            raise SchemaError(f"{where}: not an object")
        extra = [key for key in value if key not in spec.fields]
        if extra:
            raise SchemaError(f"{where}: {len(extra)} key(s) the schema doesn't allow")
        missing = [key for key in spec.fields if key not in value and key not in spec.optional]
        if missing:
            raise SchemaError(f"{where}: missing {', '.join(sorted(missing))}")
        for key, item in value.items():
            validate(item, spec.fields[key], f"{where}.{key}")
        return
    if isinstance(spec, Map):
        if not isinstance(value, dict):
            raise SchemaError(f"{where}: not an object")
        for key, item in value.items():
            if not isinstance(key, str) or not _text_ok(key, spec.keys):
                raise SchemaError(f"{where}: a key the schema doesn't allow")
            validate(item, spec.value, f"{where}.{key}")
        return
    if isinstance(spec, Seq):
        if not isinstance(value, list) or len(value) > spec.most:
            raise SchemaError(f"{where}: not a list of the right size")
        for index, item in enumerate(value):
            validate(item, spec.item, f"{where}[{index}]")
        return
    raise SchemaError(f"{where}: unknown schema")


def _text_ok(value: str, spec: Text) -> bool:
    return value in spec.values or bool(spec.pattern is not None and spec.pattern.fullmatch(value))


# The fixed vocabularies (each from Row-Bot's own contracts or constants, never from data).
KINDS = enum("document_lookups", "structured_transforms", "small_coding", "tool_workflows", "other")
FAMILY_KEYS = enum("document_lookups", "structured_transforms", "small_coding", "tool_workflows", "own_work")
FIDELITY = enum("exact", "redacted", "partial")
DECISION_KINDS = enum("model_call", "tool_call", "approval")
DECISION_STATUS = enum("pending", "ok", "error", "denied", "cancelled", "stopped", "timeout")
EPISODE_SOURCES = enum("my_chat", "channel_chat", "workflow", "goal")
EPISODE_STATUS = enum("running", "completed", "failed", "stopped", "cancelled")
SIGNAL_KINDS = enum("teach", "next_message_correction", "approval_denied", "stop", "retry")
PURPOSES = enum("learning_diagnosis", "learning_test", "learning_teacher", "learning_meta", "other")
REPLAY_SKIPS = enum("not_ok", "held_out", "untrusted", "partial_without_correction", "partial_needs_exact_prefix",
                    "known_bad", "no_reference", "purged", "other")
RUN_STATUS = enum("passed", "failed", "budget", "error", "unverified", "skipped", "not_run")
CAUSES = enum("provider_error", "malformed_call", "harness", "budget", "no_tool", "tool_not_found", "bad_call",
              "file_write", "no_answer", "wrong_answer", "other")
VERSION_STATES = enum("ready", "couldnt_start", "not_ready")
SPLITS = enum("real_uncorrected", "real_corrected", "packs")
ARMS = enum("start", "personal", "main")
CONTRASTS = enum("personal vs start", "main vs start")
RUNTIMES = enum("row_bot", "ollama", "lmstudio", "llama_cpp", "vllm")

RUN = Obj({
    "on": Text(pattern=DAY), "build": Maybe(Text(pattern=COMMIT)), "build_clean": BOOL,
    "script_sha256": Text(pattern=SHA256), "code_sha256": Text(pattern=SHA256), "code_files": COUNT,
    "copy": Obj({"files": COUNT, "bytes": COUNT, "links_skipped": COUNT}), "minutes": HOURS,
})
VERSION_ROW = Obj({"version": COUNT, "trained_on": Maybe(Text(pattern=DAY)), "state": VERSION_STATES,
                   "pilot": BOOL, "examples": COUNT, "kept_pass": Maybe(COUNT), "passes": Maybe(COUNT),
                   "training_hours": Maybe(HOURS)})
HEALTH = Obj({
    "format": enum(FORMAT), "schema_version": Num(integer=True, low=SCHEMA_VERSION, high=SCHEMA_VERSION),
    "mode": enum("health"), "run": RUN,
    "recording": Obj({"first_day": Maybe(Text(pattern=DAY)), "last_day": Maybe(Text(pattern=DAY)), "days": COUNT,
                      "live_episodes": COUNT, "imported_episodes": COUNT, "sources": Map(EPISODE_SOURCES, COUNT)}),
    "decisions": Obj({"total": COUNT, "fidelity": Map(FIDELITY, COUNT), "kind": Map(DECISION_KINDS, COUNT),
                      "status": Map(DECISION_STATUS, COUNT)}),
    "signals": Obj({"total": COUNT, "withdrawn": COUNT, "kind": Map(SIGNAL_KINDS, COUNT)}),
    "hidden_problems": Obj({key: COUNT for key in ("vault", "pack_tasks", "uncorrected", "corrected", "exact",
                                                   "redacted", "retired", "corrections_waiting")}),
    "corrections": Obj({"confirmed_live": COUNT, "held_out": COUNT, "counted_for_holding_out": COUNT}),
    "versions": Obj({"total": COUNT, "ready": COUNT, "couldnt_start": COUNT, "not_ready": COUNT, "pilots": COUNT,
                     "list": Seq(VERSION_ROW, 500)}),
    "learning_tokens": Map(Text(pattern=MONTH), Obj({"cloud": COUNT, "local": COUNT,
                                                     "purposes": Map(PURPOSES, Obj({"cloud": COUNT,
                                                                                     "local": COUNT}))})),
    "exclusions": Obj({"partial_decisions": COUNT, "opted_out_chats": COUNT, "pilot_versions": COUNT,
                       "not_replayable": Map(REPLAY_SKIPS, COUNT)}),
    "capture": Obj({"incomplete_episodes": COUNT, "incomplete_decisions": COUNT, "failed_calls": COUNT,
                    "episodes": Map(EPISODE_STATUS, COUNT)}),
    "lessons": Obj({"total": COUNT, "in_use": COUNT}),
    "ways_of_working": Obj({"total": COUNT, "in_use": COUNT}),
})

ANALYSIS = Obj({
    "problems": COUNT, "clusters": COUNT, "resamples": COUNT, "permutations": COUNT, "seed": COUNT,
    "difference": Maybe(SIGNED), "low": Maybe(SIGNED), "high": Maybe(SIGNED), "p_value": Maybe(SHARE),
    "exact": BOOL, "baseline": Maybe(SHARE), "arm": Maybe(SHARE), "enough": BOOL,
    "interval_verdict": enum("better", "worse", "unclear"), "not_worse": BOOL,
})
SPLIT_ANALYSIS = Obj({
    "primary": ANALYSIS, "majority_vote": ANALYSIS, "unclustered": ANALYSIS, "left_pair": COUNT,
    "by_kind": Map(KINDS, ANALYSIS), "exact_only": ANALYSIS, "per_main_model": Map(Text(pattern=MODEL_ORDINAL), ANALYSIS),
}, optional=frozenset({"exact_only", "per_main_model"}))
ARM_SPLIT = Obj({"problems": COUNT, "counted": COUNT, "mean": Maybe(SHARE), "runs": Map(RUN_STATUS, COUNT),
                 "causes": Map(CAUSES, COUNT)})
TEST_PART = Obj({"split": SPLITS, "problems": COUNT, "clusters": COUNT, "difference": Maybe(SIGNED),
                 "low": Maybe(SIGNED), "high": Maybe(SIGNED), "p_value": Maybe(SHARE), "p_holm": Maybe(SHARE),
                 "margin": Maybe(SIGNED), "met": Maybe(BOOL)})
HYPOTHESIS = Obj({
    "state": enum("tested", "not_tested", "not_testable"),
    "reason": Maybe(enum("too_few_problems", "no_version", "no_starting_model", "runtime_unavailable", "stopped",
                         "no_lessons_or_ways_of_working", "arms_not_built_yet")),
    "superiority": Maybe(TEST_PART), "non_inferiority": Maybe(TEST_PART), "supported": Maybe(BOOL),
    "sensitivity": Maybe(Obj({"exact_only": ANALYSIS, "majority_vote": ANALYSIS, "unclustered": ANALYSIS,
                              "per_main_model": Map(Text(pattern=MODEL_ORDINAL), ANALYSIS),
                              "corrected_problems": ANALYSIS, "checker_versions": COUNT})),
    "by_kind": Maybe(Map(KINDS, ANALYSIS)),
    "counts": Maybe(Obj({"lessons_in_use": COUNT, "ways_of_working_in_use": COUNT, "corrected_problems": COUNT})),
})
FREEZE = Obj({
    "format": enum(FORMAT), "schema_version": Num(integer=True, low=SCHEMA_VERSION, high=SCHEMA_VERSION),
    "mode": enum("freeze"), "run": RUN,
    "design": Obj({"as_of": Text(pattern=DAY), "metric_version": COUNT, "repeats": COUNT, "seed": COUNT,
                   "resamples": COUNT, "permutations": COUNT, "task_tokens": COUNT, "runtime": RUNTIMES,
                   "serve_context": Maybe(COUNT), "container": BOOL, "main_reference": BOOL, "stopped": BOOL,
                   "hours": HOURS, "cloud_tokens": COUNT, "local_tokens": COUNT,
                   "vault_refresh": Obj({"considered": COUNT, "added": COUNT, "retired": COUNT, "held": COUNT})}),
    "versions": Obj({"total": COUNT, "pilots": COUNT, "candidates": COUNT, "evaluated": Maybe(COUNT),
                     "trained_on": Maybe(Text(pattern=DAY)), "kept_pass": Maybe(COUNT), "passes": Maybe(COUNT)}),
    "problems": Obj({"real_uncorrected": COUNT, "real_corrected": COUNT, "packs": COUNT, "after_as_of": COUNT,
                     "main_models": COUNT}),
    "arms": Map(ARMS, Obj({"tokens": COUNT, "cloud_tokens": COUNT, "seconds": HOURS, "ran": COUNT, "cached": COUNT,
                           "identical": Obj({"problems": COUNT, "identical": COUNT}),
                           "splits": Map(SPLITS, ARM_SPLIT)})),
    "contrasts": Map(CONTRASTS, Map(SPLITS, SPLIT_ANALYSIS)),
    "hypotheses": Obj({"H1": HYPOTHESIS, "H2": HYPOTHESIS}),
    "holm": Obj({"family": Seq(enum(*FAMILY), 2), "alpha": SHARE, "untested_enter_as": SHARE,
                 "adjusted": Map(enum(*FAMILY), Maybe(SHARE))}),
})

THINKING_MODELS = enum("start", "B", "A", "A32")
THINKING_PART = Obj({"overall": ANALYSIS, "by_kind": Map(FAMILY_KEYS, ANALYSIS)})
THINKING = Obj({
    "format": enum(FORMAT), "schema_version": Num(integer=True, low=SCHEMA_VERSION, high=SCHEMA_VERSION),
    "mode": enum("thinking"), "run": RUN,
    "design": Obj({"visible_repeats": COUNT, "hidden_repeats": COUNT, "container": BOOL, "hours": HOURS,
                   "complete": BOOL}),
    "rows": Map(THINKING_MODELS, Obj({"fit": COUNT, "rows_measured": COUNT, "too_long": COUNT, "examples": COUNT})),
    "training": Map(THINKING_MODELS, Obj({"trained": BOOL, "examples": COUNT, "steps": Maybe(COUNT),
                                          "gpu_hours": Maybe(HOURS)})),
    "arms": Map(Text(pattern=re.compile(r"^(start|B|A|A32)-(on|off)$")),
                Obj({"seconds_per_task": Maybe(HOURS), "tokens": COUNT})),
    "thinking_on_vs_off": Map(THINKING_MODELS, Map(enum("visible", "hidden", "combined"), THINKING_PART)),
})
SCHEMAS = {"health": HEALTH, "freeze": FREEZE, "thinking": THINKING}


# ── 4. Small helpers over the copy (it is the only data folder Row-Bot sees) ──


def _day(at: float | None) -> str | None:
    if not at:
        return None
    return datetime.fromtimestamp(float(at)).strftime("%Y-%m-%d")


def _counts(values: Iterable[str], allowed: Text) -> dict[str, int]:
    """A Counter over fixed labels; anything else counts as "other" (or is dropped if "other" isn't one)."""

    found: Counter[str] = Counter()
    for value in values:
        key = value if value in allowed.values else "other"
        if key in allowed.values:
            found[key] += 1
    return dict(sorted(found.items()))


def _versions(copy: Path) -> list[dict[str, Any]]:
    """Every version folder's facts (no file name, no model name, no id)."""

    from row_bot.learning.store import LearningStore
    from row_bot.learning.training import releases

    store = LearningStore(copy)
    rows = []
    folder = releases.versions_dir(copy)
    for entry in sorted(folder.iterdir() if folder.is_dir() else (), key=lambda item: item.name):
        if not (entry.is_dir() and entry.name.startswith("v") and entry.name[1:].isdigit()):
            continue
        state = releases.read_state(entry)
        if state is None:
            continue
        try:
            dataset = json.loads((entry / releases.DATASET_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            dataset = {}
        rendering = str(((dataset or {}).get("rendering") or {}).get("name") or "")
        if state.phase == releases.READY:
            phase = "ready"
        elif releases.couldnt_start(state):
            phase = "couldnt_start"
        else:
            phase = "not_ready"
        kept = dict(state.kept_pass or {})
        hours = None
        job = store.get("job", state.job_id) if state.job_id and store.exists() else None
        if job is not None and getattr(job, "steps_done", 0) and getattr(job, "seconds_per_step", None):
            hours = round(job.steps_done * job.seconds_per_step / 3600, 3)
        rows.append({
            "version": int(state.version), "trained_on": _day(state.trained_at), "state": phase,
            # §8.4: versions trained before the rows took the serving shape are pilots.
            "pilot": not rendering.startswith("serving-"), "examples": int(state.examples or 0),
            "kept_pass": int(kept["pass"]) if isinstance(kept.get("pass"), int) else None,
            "passes": int(kept["of"]) if isinstance(kept.get("of"), int) else None, "training_hours": hours,
            # Kept apart (never written): what the freeze serves.
            "_gguf": (entry / state.gguf) if state.gguf else None, "_runtime": state.runtime,
            "_ready_once": bool(state.phase == releases.READY or state.verified_at),
            "_serve_context": int(((dataset or {}).get("rendering") or {}).get("serve_context") or 0) or None,
            "_base": str((((_read_json(entry / releases.MANIFEST_FILE).get("identity") or {}).get("base") or {})
                          .get("name") or "")),
        })
    return rows


def _read_json(path: Path) -> dict[str, Any]:
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return found if isinstance(found, dict) else {}


def _public(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in row.items() if not key.startswith("_")}


# ── 5. --health: counts and errors only (§10) ───────────────────────────────


def health_report(copy: Path) -> dict[str, Any]:
    """Counts and errors from the copy: never a pass rate, a comparison or anything a check decided."""

    from row_bot.learning import evaluation, holdout
    from row_bot.learning.contracts import Decision, Episode, LessonRecord, Release, Signal
    from row_bot.learning.store import LearningStore

    store = LearningStore(copy)
    episodes = [item for item in store.find("episode", limit=10_000_000) if isinstance(item, Episode)]
    decisions = [item for item in store.find("decision", limit=10_000_000) if isinstance(item, Decision)]
    signals = [item for item in store.find("signal", limit=10_000_000) if isinstance(item, Signal)]
    live = [item for item in episodes if not item.historical]
    days = sorted({_day(item.started_at) for item in live if item.started_at})
    historical = {item.id for item in episodes if item.historical}
    corrections = [item for item in signals if item.kind in holdout.CORRECTION_KINDS and item.decision_id
                   and item.withdrawn_at is None]
    by_id = {item.id: item for item in decisions}
    confirmed_live = sum(1 for item in corrections if (found := by_id.get(str(item.decision_id))) is not None
                         and found.fidelity in ("exact", "redacted") and found.episode_id not in historical)
    try:
        counted = len(json.loads(store.meta_value("held_out_corrections.counted", "[]")))
    except ValueError:
        counted = 0
    versions = _versions(copy)
    lessons = [item for item in store.find("lesson", limit=1_000_000) if isinstance(item, LessonRecord)]
    behaviours = [item for item in store.find("release", limit=1_000_000)
                  if isinstance(item, Release) and item.kind == "behaviour"]
    replay = evaluation.collect_replay(store, refresh=False, limit=10_000_000)
    return {
        "format": FORMAT, "schema_version": SCHEMA_VERSION, "mode": "health",
        "recording": {
            "first_day": days[0] if days else None, "last_day": days[-1] if days else None, "days": len(days),
            "live_episodes": len(live), "imported_episodes": len(episodes) - len(live),
            "sources": _counts((item.source for item in live), EPISODE_SOURCES),
        },
        "decisions": {"total": len(decisions), "fidelity": _counts((item.fidelity for item in decisions), FIDELITY),
                      "kind": _counts((item.kind for item in decisions), DECISION_KINDS),
                      "status": _counts((item.status for item in decisions), DECISION_STATUS)},
        "signals": {"total": len(signals), "withdrawn": sum(1 for item in signals if item.withdrawn_at is not None),
                    "kind": _counts((item.kind for item in signals), SIGNAL_KINDS)},
        "hidden_problems": evaluation.audit_counts(asked_by="the paper's health check", store=store),
        "corrections": {"confirmed_live": confirmed_live, "held_out": len(store.held_out_correction_ids()),
                        "counted_for_holding_out": counted},
        "versions": {
            "total": len(versions), "ready": sum(1 for row in versions if row["state"] == "ready"),
            "couldnt_start": sum(1 for row in versions if row["state"] == "couldnt_start"),
            "not_ready": sum(1 for row in versions if row["state"] == "not_ready"),
            "pilots": sum(1 for row in versions if row["pilot"]), "list": [_public(row) for row in versions],
        },
        "learning_tokens": learning_tokens(copy),
        "exclusions": {"partial_decisions": sum(1 for item in decisions if item.fidelity == "partial"),
                       "opted_out_chats": len(store.opted_out_chats()),
                       "pilot_versions": sum(1 for row in versions if row["pilot"]),
                       "not_replayable": _counts(replay.skipped.elements(), REPLAY_SKIPS)},
        "capture": {"incomplete_episodes": sum(1 for item in episodes if item.incomplete),
                    "incomplete_decisions": sum(1 for item in decisions if item.incomplete),
                    "failed_calls": sum(1 for item in decisions if item.status in ("error", "timeout")),
                    "episodes": _counts((item.status for item in episodes), EPISODE_STATUS)},
        "lessons": {"total": len(lessons), "in_use": sum(1 for item in lessons if item.status == "in_use")},
        "ways_of_working": {"total": len(behaviours),
                            "in_use": sum(1 for item in behaviours if item.status in ("in_use", "trial"))},
    }


def learning_tokens(copy: Path) -> dict[str, Any]:
    """Learning's own model calls in the usage ledger, per calendar month: cloud and local tokens."""

    import sqlite3

    from row_bot.learning.spending import LEARNING_PURPOSES, is_local_provider

    ledger = copy / "usage.db"
    if not ledger.is_file():
        return {}
    marks = ", ".join("?" for _ in LEARNING_PURPOSES)
    with contextlib.closing(sqlite3.connect(f"{ledger.resolve().as_uri()}?mode=ro", uri=True)) as connection:
        rows = connection.execute(
            "SELECT created_at, provider, purpose, COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)"
            f" FROM model_usage WHERE purpose IN ({marks})", LEARNING_PURPOSES).fetchall()
    months: dict[str, Any] = {}
    local_cache: dict[str, bool] = {}
    for created_at, provider, purpose, tokens in rows:
        month = datetime.fromtimestamp(float(created_at or 0)).strftime("%Y-%m")
        where = "local" if local_cache.setdefault(str(provider), is_local_provider(str(provider or ""))) else "cloud"
        entry = months.setdefault(month, {"cloud": 0, "local": 0, "purposes": {}})
        entry[where] += int(tokens or 0)
        name = purpose if purpose in PURPOSES.values else "other"
        part = entry["purposes"].setdefault(name, {"cloud": 0, "local": 0})
        part[where] += int(tokens or 0)
    return dict(sorted(months.items()))


# ── 6. --freeze: the pre-registered freeze evaluation (§7) ──────────────────


@dataclass
class Prepared:
    """The arms the freeze runs, served from the copy (model references never leave this script)."""

    refs: dict[str, str]
    serve_context: int | None
    runtime: str
    cleanup: Callable[[], None] = lambda: None
    reason: str | None = None


def serve_arms(copy: Path, version: Mapping[str, Any]) -> Prepared:
    """Serve the version and its untouched starting model from the copy's own files, under the copy's own
    names (never the profile's), the same way and in the same runtime, thinking off (§4)."""

    from row_bot.learning.training import base_model, recipes, serving, weights

    runtime = str(version.get("_runtime") or "ollama")
    context = version.get("_serve_context") or recipes.default().serve_context
    model = next((item for item in weights.MODELS.values() if item.repo == version.get("_base")), None)
    base_gguf = None
    if model is not None:
        state = base_model.read(model.id, copy)
        if state is not None and state.gguf and (base_model.folder(model.id, copy) / state.gguf).is_file():
            base_gguf = base_model.folder(model.id, copy) / state.gguf
    if base_gguf is None:
        return Prepared({}, context, runtime, reason="no_starting_model")
    number = int(version["version"])
    made: list[Callable[[], None]] = []

    def cleanup() -> None:
        for undo in reversed(made):
            with contextlib.suppress(Exception):
                undo()

    start = serving.serve(runtime, version=number, gguf=base_gguf, serve_context=int(context),
                          folder=base_model.folder(model.id, copy), root=copy, tag=base_model.tag(copy),
                          tool_turn=False)
    if start.state == "ready":
        made.append(lambda: base_model.withdraw(model.id, root=copy))
    personal = serving.serve(runtime, version=number, gguf=Path(version["_gguf"]), serve_context=int(context),
                             folder=Path(version["_gguf"]).parent, root=copy, tool_turn=False)
    if personal.state == "ready":
        made.append(lambda: serving.withdraw(runtime, number, root=copy))
    if start.state != "ready" or personal.state != "ready" or not start.model_ref or not personal.model_ref:
        cleanup()
        reasons = {start.reason, personal.reason}
        reason = ("runtime_unavailable" if reasons & {"not_running", "not_installed", "not_local"} else
                  "no_starting_model" if start.state != "ready" else "no_version")
        return Prepared({}, context, runtime, reason=reason)
    return Prepared({"start": start.model_ref, "personal": personal.model_ref}, int(context), runtime, cleanup)


@dataclass
class Hooks:
    """What a test replaces: serving the arms, the pack runner and the replay client (fakes, no GPU)."""

    prepare: Callable[[Path, Mapping[str, Any]], Prepared] = serve_arms
    runner: Callable[[Sequence[Any]], Any] | None = None
    client: Any = None
    container: Callable[[], bool] | None = None
    main_ref: Callable[[], str] | None = None
    ask: Callable[[str], str] = input
    installed: Callable[[], bool] = installed_app_running


@contextlib.contextmanager
def _worker_runner(arms: Sequence[Any], copy: Path) -> Iterator[Any]:
    """Row-Bot's evaluation worker running pack tasks as real Agent turns (O56), as the product does."""

    from row_bot.learning import eval_worker

    worker_arms = {arm.name: eval_worker.WorkerArm(arm.name, str(arm.model_ref), arm.serve_context is not None)
                   for arm in arms}
    credentials, providers, unsupported = eval_worker.gather(list(worker_arms.values()))
    spec = {"arms": [{"name": arm.name, "model_ref": arm.model_ref} for arm in arms],
            "tools": eval_worker.safe_tools(eval_worker.person_tools()), "providers": providers,
            "code_backend": "container"}
    worker = eval_worker.EvalWorker(spec, credentials=credentials, person_data=copy)
    try:
        worker.start()
        yield eval_worker.RuntimeRunner(worker, worker_arms, record_usage=None, unsupported=set(unsupported))
    finally:
        worker.stop()


def freeze_report(copy: Path, as_of: date, *, hooks: Hooks, main_reference: bool = False,
                  cap_tokens: int = CLOUD_CAP, repeats: int = 3) -> dict[str, Any]:
    from row_bot.learning import evaluation
    from row_bot.learning.contracts import LessonRecord, Release
    from row_bot.learning.packs import PACKS
    from row_bot.learning.spending import TokenMeter
    from row_bot.learning.store import LearningStore

    started = time.monotonic()
    store = LearningStore(copy)
    # The vault takes its share of decisions it hasn't considered yet, by the product's own rule (on the copy).
    refreshed = evaluation.refresh_audit(store=store, asked_by="the paper's freeze evaluation")
    versions = _versions(copy)
    candidates = [row for row in versions if not row["pilot"] and row["_ready_once"] and row["_gguf"] is not None
                  and Path(row["_gguf"]).is_file()]
    newest = max(candidates, key=lambda row: row["version"]) if candidates else None
    lessons = sum(1 for item in store.find("lesson", limit=1_000_000)
                  if isinstance(item, LessonRecord) and item.status == "in_use")
    behaviours = sum(1 for item in store.find("release", limit=1_000_000)
                     if isinstance(item, Release) and item.kind == "behaviour" and item.status in ("in_use", "trial"))
    cutoff = datetime.combine(as_of + timedelta(days=1), datetime.min.time()).timestamp() - 1e-6
    container = bool((hooks.container or _container)())
    packs = [pack for pack in PACKS if container or pack != "small_coding"]
    verifiers = None if container else ["answer", "json_answer", "file", "app_state"]
    prepared = hooks.prepare(copy, newest) if newest is not None else Prepared({}, None, "ollama", reason="no_version")
    result: dict[str, Any] | None = None
    try:
        arms = []
        if prepared.refs:
            arms = [evaluation.Arm("start", model_ref=prepared.refs["start"], subject_kind="baseline",
                                   serve_context=prepared.serve_context),
                    evaluation.Arm("personal", model_ref=prepared.refs["personal"], subject_kind="release",
                                   serve_context=prepared.serve_context)]
            contrasts = [("start", "personal")]
            if main_reference:
                from row_bot.learning import routing

                main = (hooks.main_ref or routing.main_model_ref)()
                if main:
                    arms.append(evaluation.Arm("main", model_ref=main, cache=True))
                    contrasts.append(("start", "main"))
            progress = _Progress(started)
            with (contextlib.nullcontext(hooks.runner(arms)) if hooks.runner is not None
                  else _worker_runner(arms, copy)) as runner:
                result = evaluation.freeze_check(
                    arms, as_of=cutoff, asked_by="the paper's freeze evaluation", contrasts=contrasts,
                    client=hooks.client, budget=evaluation.Budget(task_tokens=150_000), meter=TokenMeter(cap_tokens),
                    store=store, runner=runner, cache=evaluation.ArmCache(copy), packs=packs, verifiers=verifiers,
                    repeats=repeats, on_progress=progress)
    finally:
        prepared.cleanup()
    return assemble_freeze(result, as_of=as_of, versions=versions, newest=newest, prepared=prepared,
                           refreshed=refreshed, container=container, main_reference=main_reference,
                           lessons=lessons, behaviours=behaviours, hours=(time.monotonic() - started) / 3600)


def _container() -> bool:
    from row_bot.learning.sandbox import container_runtime

    return container_runtime() is not None


class _Progress:
    """Counts only: how many runs are done, every few minutes."""

    def __init__(self, started: float) -> None:
        self.started = started
        self.last = 0.0

    def __call__(self, done: int, total: int) -> None:
        now = time.monotonic()
        if done == total or now - self.last >= 120:
            self.last = now
            say(f"  {done} of {total} runs ({round((now - self.started) / 60)} min)")


def _summary(found: Mapping[str, Any] | None) -> dict[str, Any] | None:
    return None if found is None else {key: found[key] for key in ANALYSIS.fields}


def assemble_freeze(result: Mapping[str, Any] | None, *, as_of: date, versions: Sequence[Mapping[str, Any]],
                    newest: Mapping[str, Any] | None, prepared: Prepared, refreshed: Mapping[str, int],
                    container: bool, main_reference: bool, lessons: int, behaviours: int,
                    hours: float) -> dict[str, Any]:
    """The freeze's JSON from ``evaluation.freeze_check``'s aggregates: the design, the arms, every contrast,
    H1 and H2 with the pre-registered rules (§5, §7), and Holm across {H1, H2}."""

    from row_bot.learning import evaluation

    result = dict(result or {})
    contrast = (result.get("contrasts") or {}).get("personal vs start") or {}
    corrected = int((result.get("problems") or {}).get("real_corrected") or 0)
    h1: dict[str, Any] = {"state": "not_testable", "reason": prepared.reason or "no_version", "superiority": None,
                          "non_inferiority": None, "supported": None, "sensitivity": None, "by_kind": None,
                          "counts": None}
    p_h1 = None
    if contrast:
        primary = contrast["real_uncorrected"]["primary"]
        packs = contrast["packs"]["primary"]
        enough = primary["problems"] >= evaluation.MIN_PROBLEMS
        stopped = bool(result.get("stopped"))
        p_h1 = primary["p_value"] if enough and not stopped else None
        h1.update({
            "state": "tested" if p_h1 is not None else "not_tested",
            "reason": None if p_h1 is not None else ("stopped" if stopped else "too_few_problems"),
            "superiority": _part("real_uncorrected", primary),
            "non_inferiority": {**_part("packs", packs), "margin": -MARGIN,
                                "met": packs["low"] is not None and packs["low"] > -MARGIN},
            "sensitivity": {
                "exact_only": _summary(contrast["real_uncorrected"]["exact_only"]),
                "majority_vote": _summary(contrast["real_uncorrected"]["majority_vote"]),
                "unclustered": _summary(contrast["real_uncorrected"]["unclustered"]),
                "per_main_model": {key: _summary(value) for key, value in
                                   contrast["real_uncorrected"]["per_main_model"].items()},
                "corrected_problems": _summary(contrast["real_corrected"]["primary"]),
                "checker_versions": 1,
            },
            "by_kind": {key: _summary(value) for key, value in contrast["real_uncorrected"]["by_kind"].items()},
        })
    if lessons or behaviours:
        h2_reason = "arms_not_built_yet"  # the arms with lessons on and off come with L3b's application of them
    else:
        h2_reason = "no_lessons_or_ways_of_working"
    h2 = {"state": "not_testable", "reason": h2_reason, "superiority": None, "non_inferiority": None,
          "supported": None, "sensitivity": None, "by_kind": None,
          "counts": {"lessons_in_use": lessons, "ways_of_working_in_use": behaviours, "corrected_problems": corrected}}
    adjusted = evaluation.holm({"H1": p_h1, "H2": None}, family=FAMILY)
    if h1["superiority"] is not None:
        h1["superiority"]["p_holm"] = adjusted["H1"] if p_h1 is not None else None
        met = p_h1 is not None and adjusted["H1"] < ALPHA and (h1["superiority"]["low"] or 0) > 0
        h1["superiority"]["met"] = met if p_h1 is not None else None
        h1["supported"] = (met and bool(h1["non_inferiority"]["met"])) if p_h1 is not None else None
    arms = {}
    for name, row in (result.get("arms") or {}).items():
        arms[name] = {key: row[key] for key in ("tokens", "cloud_tokens", "seconds", "ran", "cached", "identical")}
        arms[name]["splits"] = {split: {**part, "runs": _counts(Counter(part["runs"]).elements(), RUN_STATUS),
                                        "causes": _counts(Counter(part["causes"]).elements(), CAUSES)}
                                for split, part in row["splits"].items()}
    contrasts = {name: {split: _split(part) for split, part in value.items()}
                 for name, value in (result.get("contrasts") or {}).items()}
    problems = result.get("problems") or {}
    candidates = [row for row in versions if not row["pilot"] and row["_ready_once"]]
    return {
        "format": FORMAT, "schema_version": SCHEMA_VERSION, "mode": "freeze",
        "design": {
            "as_of": as_of.isoformat(), "metric_version": evaluation.METRIC_VERSION,
            "repeats": int(result.get("repeats") or 0), "seed": evaluation.ANALYSIS_SEED,
            "resamples": evaluation.RESAMPLES, "permutations": evaluation.PERMUTATIONS,
            "task_tokens": int(((result.get("budget") or {}).get("task_tokens")) or 150_000),
            "runtime": prepared.runtime, "serve_context": prepared.serve_context, "container": container,
            "main_reference": main_reference, "stopped": bool(result.get("stopped")), "hours": round(hours, 3),
            "cloud_tokens": sum(row["cloud_tokens"] for row in arms.values()),
            "local_tokens": sum(row["tokens"] - row["cloud_tokens"] for row in arms.values()),
            "vault_refresh": {key: int(refreshed.get(key, 0)) for key in ("considered", "added", "retired", "held")},
        },
        "versions": {"total": len(versions), "pilots": sum(1 for row in versions if row["pilot"]),
                     "candidates": len(candidates), "evaluated": int(newest["version"]) if newest and contrast else None,
                     "trained_on": newest["trained_on"] if newest and contrast else None,
                     "kept_pass": newest["kept_pass"] if newest and contrast else None,
                     "passes": newest["passes"] if newest and contrast else None},
        "problems": {"real_uncorrected": int(problems.get("real_uncorrected") or 0), "real_corrected": corrected,
                     "packs": int(problems.get("packs") or 0), "after_as_of": int(problems.get("after_as_of") or 0),
                     "main_models": int(result.get("main_models") or 0)},
        "arms": arms, "contrasts": contrasts, "hypotheses": {"H1": h1, "H2": h2},
        "holm": {"family": list(FAMILY), "alpha": ALPHA, "untested_enter_as": 1.0,
                 "adjusted": {"H1": adjusted["H1"] if p_h1 is not None else None, "H2": None}},
    }


def _part(split: str, found: Mapping[str, Any]) -> dict[str, Any]:
    return {"split": split, "problems": found["problems"], "clusters": found["clusters"],
            "difference": found["difference"], "low": found["low"], "high": found["high"],
            "p_value": found["p_value"], "p_holm": None, "margin": None, "met": None}


def _split(part: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"primary": _summary(part["primary"]), "majority_vote": _summary(part["majority_vote"]),
                           "unclustered": _summary(part["unclustered"]), "left_pair": int(part["left_pair"]),
                           "by_kind": {key: _summary(value) for key, value in part["by_kind"].items()}}
    if "exact_only" in part:
        out["exact_only"] = _summary(part["exact_only"])
        out["per_main_model"] = {key: _summary(value) for key, value in part["per_main_model"].items()}
    return out


# ── 7. --thinking: O97's recheck through the L2w ablation script ────────────


def run_ablation(copy: Path, out: Path) -> int:
    """``learning_shape_ablation.py --thinking both`` on the copy, its own output kept out of sight (it
    names models and files); only its results file is read, by :func:`thinking_report`."""

    env = {**os.environ, "ROW_BOT_DATA_DIR": str(copy), "ROW_BOT_LEARNING_PREVIEW": "1",
           "ROW_BOT_DESKTOP_NOTIFICATIONS": "off", "ROW_BOT_AUTO_START_OLLAMA": "0"}
    log = out / "ablation.log"
    with log.open("wb") as handle:
        process = subprocess.run([sys.executable, str(ROOT / "scripts" / "learning_shape_ablation.py"), "--data",
                                  str(copy), "--out", str(out), "--thinking", "both"], cwd=ROOT, env=env,
                                 stdout=handle, stderr=subprocess.STDOUT, check=False)
    return process.returncode


def thinking_report(results: Mapping[str, Any]) -> dict[str, Any]:
    """The ablation's results (``l2w_ablation.json``) as aggregates: rows, training, time per task and
    tokens per arm, and thinking on vs off per model (visible, hidden, both), overall and per kind."""

    rows = {}
    for name, row in (results.get("rows") or {}).items():
        if name in THINKING_MODELS.values and isinstance(row, dict):
            rows[name] = {key: int(row.get(key) or 0) for key in ("fit", "rows_measured", "too_long", "examples")}
    training = {}
    for name, row in (results.get("train") or {}).items():
        if name in THINKING_MODELS.values and isinstance(row, dict):
            training[name] = {"trained": row.get("state") == "evaluating", "examples": int(row.get("examples") or 0),
                              "steps": int(row["steps"]) if isinstance(row.get("steps"), int) else None,
                              "gpu_hours": float(row["gpu_hours"]) if isinstance(row.get("gpu_hours"), (int, float))
                              else None}
    compare = results.get("compare") or {}
    arms = {}
    for name, row in (compare.get("per_arm") or {}).items():
        if not isinstance(row, dict) or not _text_ok(str(name), THINKING.fields["arms"].keys):
            continue
        seconds = [part.get("seconds_per_run") for part in row.values() if isinstance(part, dict)]
        seconds = [value for value in seconds if isinstance(value, (int, float))]
        arms[name] = {"seconds_per_task": round(sum(seconds) / len(seconds), 2) if seconds else None,
                      "tokens": sum(int(part.get("tokens") or 0) for part in row.values() if isinstance(part, dict))}
    contrasts: dict[str, Any] = {}
    analysis = compare.get("analysis") or {}
    sources = (("visible", (analysis.get("visible") or {}).get("pairs") or {}),
               ("hidden", (analysis.get("hidden") or {}).get("pairs") or {}),
               ("combined", (analysis.get("hidden") or {}).get("combined_pairs") or {}))
    for part, pairs in sources:
        for model in THINKING_MODELS.values:
            found = pairs.get(f"{model}-on vs {model}-off")
            if not isinstance(found, dict):
                continue
            contrasts.setdefault(model, {})[part] = {
                "overall": _summary(found["overall"]),
                "by_kind": {key: _summary(value) for key, value in (found.get("by_kind") or {}).items()
                            if key in FAMILY_KEYS.values}}
    return {"format": FORMAT, "schema_version": SCHEMA_VERSION, "mode": "thinking",
            "design": {"visible_repeats": int((compare.get("repeats") or {}).get("visible") or 0),
                       "hidden_repeats": int((compare.get("repeats") or {}).get("hidden") or 0),
                       "container": bool(compare.get("container")),
                       "hours": round(float(compare.get("seconds") or 0) / 3600, 3), "complete": bool(compare)},
            "rows": rows, "training": training, "arms": arms, "thinking_on_vs_off": contrasts}


# ── 8. Showing, asking, writing ─────────────────────────────────────────────


def render(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False)


def output_name(report: Mapping[str, Any]) -> str:
    stamp = report["run"]["on"].replace("-", "")
    if report["mode"] == "freeze":
        return f"paper-freeze-asof-{report['design']['as_of'].replace('-', '')}-run-{stamp}.json"
    return f"paper-{report['mode']}-{stamp}.json"


def write_report(report: Mapping[str, Any], out: Path) -> str:
    out.mkdir(parents=True, exist_ok=True)
    name = output_name(report)
    target = out / name
    number = 2
    while target.exists():
        target = out / name.replace(".json", f"-{number}.json")
        number += 1
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(render(report) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(target)
    return target.name


@contextlib.contextmanager
def _quiet() -> Iterator[None]:
    """Row-Bot's own log lines and Python's warnings could name models, files or folders: none of them
    reach the console while the run works on the copy."""

    import warnings

    before = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        logging.disable(before)


# ── 9. The run ──────────────────────────────────────────────────────────────


def parse(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--health", action="store_true", help="counts and errors only (any time)")
    mode.add_argument("--freeze", action="store_true", help="the pre-registered freeze evaluation (§7)")
    mode.add_argument("--thinking", action="store_true", help="O97's thinking recheck (the L2w ablation)")
    parser.add_argument("--as-of", help="--freeze: the freeze date, YYYY-MM-DD (problems up to the end of that day)")
    parser.add_argument("--profile", required=True, help="the Row-Bot profile folder (read only, never written)")
    parser.add_argument("--scratch", required=True, help="where the copy goes (outside the profile and the checkout)")
    parser.add_argument("--out", required=True, help="where the JSON goes")
    parser.add_argument("--yes", action="store_true", help="write without asking")
    parser.add_argument("--keep-copy", action="store_true", help="keep the scratch copy (it is deleted otherwise)")
    parser.add_argument("--main-reference", action="store_true",
                        help="--freeze: also score the main model (a reference, not a hypothesis arm; cloud tokens)")
    args = parser.parse_args(argv)
    if args.freeze:
        try:
            args.as_of_date = date.fromisoformat(str(args.as_of or ""))
        except ValueError:
            parser.error("--freeze needs --as-of YYYY-MM-DD")
    elif args.as_of:
        parser.error("--as-of goes with --freeze")
    return args


def main(argv: Sequence[str] | None = None, *, hooks: Hooks | None = None) -> int:
    args = parse(argv)
    hooks = hooks or Hooks()
    mode = "health" if args.health else "freeze" if args.freeze else "thinking"
    profile, scratch, out = Path(args.profile).resolve(), Path(args.scratch).resolve(), Path(args.out).resolve()
    started = time.monotonic()
    say("Row-Bot Learning: the paper's report")
    code = code_hashes()
    say(f"  Script SHA-256:  {code['script_sha256']}")
    say(f"  Product SHA-256: {code['code_sha256']} ({code['code_files']} files)")
    say(f"  Build:           {code['build'] or 'unknown'}{'' if code['build_clean'] else ' (with local changes)'}")
    say("  Log these three lines in the Preview log before the run.")
    say()
    try:
        check_folders(profile, scratch, out)
        refuse_if_running(profile, installed=hooks.installed)
        scratch.mkdir(parents=True, exist_ok=True)
        target = scratch / (COPY_PREFIX + datetime.now().strftime("%Y%m%d-%H%M%S"))
        if target.exists():
            raise Refused("A copy from this second already exists in --scratch; wait a second and run it again.")
    except Refused as refused:
        say(refused.message)
        return 2
    copy = target / "data"
    report: dict[str, Any] | None = None
    status = 1
    try:
        say(f"Copying {'the Learning records the health check counts' if mode == 'health' else 'the profile'} "
            "to a new folder in --scratch ...")
        try:
            tree = copy_profile(profile, copy, (lambda path: bool(HEALTH_FILES.match(path))) if mode == "health"
                                else None)
        except CopyMismatch as mismatch:
            say(f"{mismatch.text}. Nothing was written.")
            return 1
        except Refused as refused:
            say(refused.message)
            return 2
        say(f"Copy checked: {len(tree.files)} files, {tree.bytes:,} bytes, the same as the profile"
            + (f" ({tree.links} links not copied)" if tree.links else "") + ".")
        try:
            with _quiet():
                report = _run_mode(mode, copy, target, args, hooks)
        except _ModeFailed:
            raise
        except Exception as error:  # noqa: BLE001 - its message or traceback could name data: the type only
            if os.environ.get("ROW_BOT_PAPER_TRACEBACK"):  # for runs on seeded data only, never the profile
                raise
            raise _ModeFailed(f"{type(error).__name__} (run again with --keep-copy to keep the copy)") from None
        report["run"] = {"on": date.today().isoformat(), "build": code["build"], "build_clean": code["build_clean"],
                         "script_sha256": code["script_sha256"], "code_sha256": code["code_sha256"],
                         "code_files": code["code_files"], "copy": {"files": len(tree.files), "bytes": tree.bytes,
                                                                    "links_skipped": tree.links},
                         "minutes": round((time.monotonic() - started) / 60, 1)}
        try:
            validate(report, SCHEMAS[mode])
        except SchemaError as error:
            say(f"The report didn't pass its own schema ({error}). Nothing was written.")
            return 1
        say()
        say("This is exactly what will be written:")
        say(render(report))
        say()
        if not args.yes:
            try:
                answer = hooks.ask("Write it to the --out folder? [y/N] ")
            except EOFError:
                answer = ""
            if answer.strip().lower() not in ("y", "yes"):
                say("Nothing was written.")
                return 0
        say(f"Written: {write_report(report, out)} in the --out folder.")
        status = 0
        return status
    except _ModeFailed as failed:
        say(f"The {mode} run stopped: {failed.reason}. Nothing was written.")
        return 1
    finally:
        if args.keep_copy:
            say("The scratch copy was kept (--keep-copy); delete it from the --scratch folder when done.")
        elif target.exists():
            if remove_copy(target):
                say("The scratch copy was deleted.")
            else:
                say(f"The scratch copy couldn't be deleted: remove the folder {target.name} from --scratch by hand.")


class _ModeFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _run_mode(mode: str, copy: Path, target: Path, args: argparse.Namespace, hooks: Hooks) -> dict[str, Any]:
    # Every Row-Bot module reads this folder from here on: the copy, never the profile (O68: no notifications).
    os.environ["ROW_BOT_DATA_DIR"] = str(copy)
    os.environ["ROW_BOT_LEARNING_PREVIEW"] = "1"
    os.environ["ROW_BOT_DESKTOP_NOTIFICATIONS"] = "off"
    os.environ.setdefault("ROW_BOT_AUTO_START_OLLAMA", "0")
    if str(ROOT / "src") not in sys.path:
        sys.path.insert(0, str(ROOT / "src"))
    from row_bot import learning

    learning.invalidate()
    if mode == "thinking":
        out = target / "ablation"
        out.mkdir(parents=True, exist_ok=True)
        say("Running the thinking recheck (the L2w ablation, --thinking both); this takes hours ...")
        code = run_ablation(copy, out)
        results = _read_json(out / "l2w_ablation.json")
        if code != 0 and not results.get("compare"):
            raise _ModeFailed(f"the ablation ended with exit code {int(code)} before its comparison")
        return thinking_report(results)
    if mode == "health":
        say("Counting ...")
        return health_report(copy)
    if not learning.read_settings(copy).enabled:
        raise _ModeFailed("Learning is off in this profile")
    say(f"Running the freeze evaluation on every hidden problem up to {args.as_of_date.isoformat()} ...")
    return freeze_report(copy, args.as_of_date, hooks=hooks, main_reference=bool(args.main_reference))


if __name__ == "__main__":
    raise SystemExit(main())
