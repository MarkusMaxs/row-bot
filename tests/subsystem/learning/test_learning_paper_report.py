"""The paper's report (P1, pre-registration §3.4, §7, §10): the participant's own script. These tests are
its privacy guarantee, so they run in the pull-request lane.

On a profile seeded with canary strings (in requests, replies, tool arguments, Teach notes, lessons, file
names, thread ids, model names and version files) neither what the script prints nor the JSON it writes holds
a canary, a path or an id; the JSON follows a strict schema and an extra key stops the run; ``--health``
holds no rate and nothing a comparison decided; the run works on a checked copy and the profile is
byte-for-byte untouched; it refuses while Row-Bot holds the profile's lock; it asks before writing; the
freeze applies the minimum size (30 problems) and Holm across {H1, H2}; the thinking recheck keeps only
aggregates of the ablation's results."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage

# The serving shape imports the Agent, which opens its conversations database where the data folder is when
# it is first imported: here, the session's test folder, never a copy the script deletes.
import row_bot.agent  # noqa: F401
from row_bot.learning import evaluation
from row_bot.learning.evaluation import Action, Arm, CaseResult, ReplayCase
from row_bot.learning.packs import visible_tasks
from row_bot.learning.spending import TokenMeter
from row_bot.learning.store import LearningStore
from row_bot.prompt_cache import ModelUsage
from row_bot.providers.usage_ledger import UsageRecord, record_model_usage
from scripts import learning_paper_report as report
from tests.fixtures.learning import CapturedWork, lesson, signal
from tests.fixtures.learning_eval import ScriptedClient

pytestmark = [pytest.mark.subsystem, pytest.mark.platform]

CANARY = "zq7canary"
# A number only a comparison holds: it must never reach the health check's output.
COMPARED = 7_654_321


# ── A profile seeded with canaries ──────────────────────────────────────────


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _version(root: Path, number: int, *, rendering: str = "serving-v1@32768", phase: str = "ready") -> None:
    folder = root / "learning_models" / "versions" / f"v{number}"
    gguf = f"{CANARY}-personal-v{number}-Q8_0.gguf"
    (folder).mkdir(parents=True, exist_ok=True)
    (folder / gguf).write_bytes(b"GGUF" + CANARY.encode())
    _write_json(folder / "state.json", {
        "version": number, "phase": phase, "job_id": f"job_{CANARY}{number}", "release_id": f"rel_{CANARY}{number}",
        "runtime": "ollama", "model_ref": f"model:ollama:row-bot-personal-v{number}:{CANARY}", "gguf": gguf,
        "trained_at": 1_792_000_000.0 + number, "examples": 90 + number, "verified_at": 1_792_000_100.0,
        "kept_pass": {"pass": 2, "of": 3, "failed": [3], "losses": [0.4, 0.3, 0.2]}})
    _write_json(folder / "dataset.json", {"rendering": {"name": rendering, "serve_context": 32768},
                                          "example_ids": [f"ex_{CANARY}"], "conversations": [f"thread-{CANARY}"]})
    _write_json(folder / "release.json", {"identity": {"base": {"name": "Qwen/Qwen3.5-2B"}},
                                          "source_thread_ids": [f"thread-{CANARY}"], "label": CANARY})
    _write_json(folder / "comparison.json", {"rows": [{"passed": {"v1": COMPARED}, "label": CANARY}],
                                             "overall": {"line": f"Better at {CANARY}"}})
    (folder / "MODEL_CARD.md").write_text(f"# {CANARY}\n", encoding="utf-8")


@pytest.fixture
def profile(writer, chats, clock, learning_on):
    """A Row-Bot profile with captured work, corrections, lessons, files, versions and Learning's ledger
    rows, every one carrying the canary."""

    work = CapturedWork(writer, chats, clock)
    answers = []
    for index in range(8):
        answers.append(work.turn(f"thread-{CANARY}-{index}", f"What is in C:\\Users\\{CANARY}\\notes-{index}.txt?",
                                 f"It says {CANARY} {index}.",
                                 args={"expression": f"{CANARY} * {index}", "path": f"/home/{CANARY}/f{index}"},
                                 model_ref=f"gpt-{CANARY}")[-1])
    store = LearningStore(learning_on)
    for number in range(2):
        store.put(signal(f"thread-{CANARY}-{number}", f"sig_{CANARY}{number}", decision_id=answers[number],
                         created_at=clock() + number, note=f"Use {CANARY}.xlsx instead"))
    store.put(lesson(f"les_{CANARY}", title=f"{CANARY} lesson", text=f"Always open {CANARY}.pdf first."))
    evaluation.refresh_audit(store=store, asked_by="test seeding")
    _version(learning_on, 1, rendering="")  # a pilot: trained before the serving shape (§8.4)
    _version(learning_on, 2)
    base = learning_on / "learning_models" / "base" / "qwen3.5-2b"
    base.mkdir(parents=True)
    (base / "base-Q8_0.gguf").write_bytes(b"GGUF")
    _write_json(base / "state.json", {"model_id": "qwen3.5-2b", "phase": "ready", "gguf": "base-Q8_0.gguf",
                                      "model_ref": f"model:ollama:row-bot-personal-base:{CANARY}"})
    (learning_on / "documents").mkdir()
    (learning_on / "documents" / f"{CANARY}-contract.docx").write_bytes(b"secret")
    for purpose, provider, tokens in (("learning_test", "codex", 1_200), ("learning_test", "ollama", 800),
                                      ("learning_diagnosis", "codex", 300)):
        record_model_usage(UsageRecord(purpose, provider, f"gpt-{CANARY}", ModelUsage(tokens, 0),
                                       thread_id=f"thread-{CANARY}", created_at=clock()))
    return learning_on


@pytest.fixture
def folders(tmp_path, monkeypatch):
    """--scratch and --out, and the process environment put back after the script changes it."""

    for name in ("ROW_BOT_LEARNING_PREVIEW", "ROW_BOT_DESKTOP_NOTIFICATIONS", "ROW_BOT_AUTO_START_OLLAMA"):
        monkeypatch.delenv(name, raising=False)
    import os

    monkeypatch.setenv("ROW_BOT_DATA_DIR", os.environ["ROW_BOT_DATA_DIR"])
    return tmp_path / "scratch", tmp_path / "out"


def _hashes(root: Path) -> dict[str, str]:
    return {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


def _ids(root: Path) -> set[str]:
    store = LearningStore(root)
    found = set()
    for kind in ("episode", "decision", "signal", "lesson"):
        for item in store.find(kind, limit=100_000):
            found |= {item.id, getattr(item, "thread_id", "") or ""}
    return {item for item in found if item}


def _assert_private(text: str, profile: Path, scratch: Path, ids: set[str]) -> None:
    assert CANARY not in text
    for path in (profile, scratch, profile.parent):
        assert str(path) not in text and path.as_posix() not in text
    for item in ids:
        assert item not in text
    assert str(COMPARED) not in text


def _run(argv: list[str], *, hooks: report.Hooks | None = None) -> int:
    return report.main(argv, hooks=hooks or report.Hooks(ask=_never_asked, installed=lambda: False))


def _never_asked(_prompt: str) -> str:
    raise AssertionError("asked although --yes was given")


def _written(out: Path) -> dict[str, Any]:
    files = list(out.glob("*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


# ── --health ────────────────────────────────────────────────────────────────


def test_health_prints_its_hash_first_then_counts_only_from_a_checked_copy(profile, folders, capsys, monkeypatch):
    scratch, out = folders
    before = _hashes(profile)
    ids = _ids(profile)
    seen: dict[str, Any] = {}
    counted = report.health_report

    def health(copy):
        import os

        from row_bot import learning

        seen.update(copy=copy, env=os.environ["ROW_BOT_DATA_DIR"], learning=learning.data_dir())
        return counted(copy)

    monkeypatch.setattr(report, "health_report", health)

    assert _run(["--health", "--profile", str(profile), "--scratch", str(scratch), "--out", str(out), "--yes"]) == 0

    printed = capsys.readouterr()
    text = printed.out + printed.err
    lines = text.splitlines()
    script_sha = hashlib.sha256(Path(report.__file__).read_bytes()).hexdigest()
    assert lines[1].split()[-1] == script_sha and "Product SHA-256" in lines[2]
    assert text.index("Script SHA-256") < text.index("Copying")
    written = _written(out)
    _assert_private(text + json.dumps(written), profile, scratch, ids)
    # It worked on the copy, and the profile is byte for byte what it was.
    assert Path(seen["copy"]).parent.parent == scratch and seen["env"] == str(seen["copy"])
    assert Path(seen["learning"]) == Path(seen["copy"])
    assert _hashes(profile) == before
    assert list(scratch.iterdir()) == [] and "The scratch copy was deleted." in text
    report.validate(written, report.HEALTH)
    assert written["decisions"]["fidelity"] == {"exact": 24}  # 8 turns: 2 model calls and a tool call each
    assert written["decisions"]["kind"] == {"model_call": 16, "tool_call": 8}
    assert written["signals"]["kind"] == {"teach": 2}
    assert written["hidden_problems"]["uncorrected"] >= 1 and written["hidden_problems"]["pack_tasks"] == 32
    assert written["versions"]["pilots"] == 1 and written["versions"]["list"][1]["kept_pass"] == 2
    month = next(iter(written["learning_tokens"].values()))
    assert (month["cloud"], month["local"]) == (1_500, 800)
    assert written["lessons"] == {"total": 1, "in_use": 0}


def test_health_holds_no_rate_and_nothing_a_comparison_decided(profile, folders):
    scratch, out = folders

    assert _run(["--health", "--profile", str(profile), "--scratch", str(scratch), "--out", str(out), "--yes"]) == 0

    def keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {key for item in value.values() for key in keys(item)}
        if isinstance(value, list):
            return {key for item in value for key in keys(item)}
        return set()

    found = keys(_written(out))
    assert not found & {"mean", "rate", "passed", "difference", "low", "high", "p_value", "baseline", "arm",
                        "interval_verdict", "verdict", "comparison", "compared"}
    assert not keys_in_schema(report.HEALTH) & {"mean", "difference", "p_value", "passed", "rate"}


def keys_in_schema(spec: Any) -> set[str]:
    if isinstance(spec, report.Obj):
        return set(spec.fields) | {key for item in spec.fields.values() for key in keys_in_schema(item)}
    if isinstance(spec, (report.Map, report.Maybe, report.Seq)):
        return keys_in_schema(getattr(spec, "value", None) or getattr(spec, "spec", None) or getattr(spec, "item"))
    return set()


def test_it_refuses_while_row_bot_holds_the_profile(profile, folders, capsys):
    from row_bot.launcher import _InstanceLock

    scratch, out = folders
    lock = _InstanceLock(profile / "launcher.lock")
    assert lock.acquire()
    try:
        code = _run(["--health", "--profile", str(profile), "--scratch", str(scratch), "--out", str(out), "--yes"])
    finally:
        lock.release()

    assert code == 2
    assert "Row-Bot is running on this profile" in capsys.readouterr().out
    assert not scratch.exists() or list(scratch.iterdir()) == []
    assert not out.exists()
    assert report.profile_in_use(profile) is False  # released: the next run may go ahead


def test_it_asks_before_writing_and_writes_nothing_without_a_yes(profile, folders, capsys):
    scratch, out = folders
    asked: list[str] = []

    def answer(prompt: str) -> str:
        asked.append(prompt)
        return "n"

    code = report.main(["--health", "--profile", str(profile), "--scratch", str(scratch), "--out", str(out)],
                       hooks=report.Hooks(ask=answer, installed=lambda: False))

    printed = capsys.readouterr().out
    assert code == 0 and len(asked) == 1 and not out.exists()
    assert "This is exactly what will be written:" in printed and "Nothing was written." in printed
    assert printed.index('"mode": "health"') < printed.index("Nothing was written.")


def test_an_extra_key_stops_the_run_and_its_name_is_never_printed(profile, folders, capsys, monkeypatch):
    scratch, out = folders
    counted = report.health_report
    monkeypatch.setattr(report, "health_report", lambda copy: {**counted(copy), f"{CANARY}-key": 1})

    code = _run(["--health", "--profile", str(profile), "--scratch", str(scratch), "--out", str(out), "--yes"])

    printed = capsys.readouterr().out
    assert code == 1 and not out.exists() and CANARY not in printed
    assert "didn't pass its own schema" in printed and list(scratch.iterdir()) == []


def test_the_schema_allows_only_fixed_keys_and_fixed_words():
    good = {"kind": {"teach": 1}, "total": 1, "withdrawn": 0}
    report.validate(good, report.HEALTH.fields["signals"])
    for bad in ({**good, "note": "x"}, {**good, "kind": {f"{CANARY}": 1}}, {**good, "total": "1"},
                {**good, "total": True}, {**good, "total": -1}):
        with pytest.raises(report.SchemaError) as raised:
            report.validate(bad, report.HEALTH.fields["signals"])
        assert CANARY not in str(raised.value)
    with pytest.raises(report.SchemaError):
        report.validate({"first_day": f"{CANARY}", "last_day": None, "days": 0, "live_episodes": 0,
                         "imported_episodes": 0, "sources": {}}, report.HEALTH.fields["recording"])


def test_a_failure_inside_the_run_names_only_its_kind(profile, folders, capsys, monkeypatch):
    scratch, out = folders
    monkeypatch.delenv("ROW_BOT_PAPER_TRACEBACK", raising=False)

    def broken(copy):
        raise OSError(f"can't open {copy}\\{CANARY}.db")

    monkeypatch.setattr(report, "health_report", broken)

    code = _run(["--health", "--profile", str(profile), "--scratch", str(scratch), "--out", str(out), "--yes"])

    printed = capsys.readouterr()
    assert code == 1 and "stopped: OSError" in printed.out and not out.exists()
    _assert_private(printed.out + printed.err, profile, scratch, set())


# ── --freeze ────────────────────────────────────────────────────────────────


class Packs:
    signature = "fake-packs-1"

    def run(self, task, arm, *, budget, meter, arm_left):
        return CaseResult(task.id, "pack", task.kind_of_work, "passed" if arm.name == "personal" else "failed",
                          "answer", tokens=400, local_tokens=400, steps=2)


def test_freeze_runs_the_newest_non_pilot_version_from_the_copy_and_reports_aggregates(profile, folders, capsys):
    scratch, out = folders
    before = _hashes(profile)
    ids = _ids(profile)
    served: dict[str, Any] = {}

    def prepare(copy, version):
        served.update(copy=copy, version=version["version"], gguf=version["_gguf"])
        return report.Prepared({"start": "model:fake:start", "personal": "model:fake:personal"}, 32768, "ollama",
                               cleanup=lambda: served.setdefault("cleaned", True))

    hooks = report.Hooks(prepare=prepare, runner=lambda arms: Packs(), container=lambda: False,
                         client=ScriptedClient(lambda *_: AIMessage(content="no idea"), local=True),
                         ask=_never_asked, installed=lambda: False)

    code = report.main(["--freeze", "--as-of", "2030-01-01", "--profile", str(profile), "--scratch", str(scratch),
                        "--out", str(out), "--yes"], hooks=hooks)

    printed = capsys.readouterr()
    assert code == 0, printed.out
    written = _written(out)
    _assert_private(printed.out + printed.err + json.dumps(written), profile, scratch, ids)
    report.validate(written, report.FREEZE)
    assert served["version"] == 2 and served["cleaned"]  # v1 is a pilot (§8.4)
    assert Path(served["gguf"]).is_relative_to(served["copy"])  # the copy's files, never the profile's
    assert _hashes(profile) == before and list(scratch.iterdir()) == []
    assert written["versions"] == {"total": 2, "pilots": 1, "candidates": 1, "evaluated": 2,
                                   "trained_on": written["versions"]["trained_on"], "kept_pass": 2, "passes": 3}
    assert written["problems"]["packs"] == 24 and written["design"]["container"] is False  # no coding without one
    h1 = written["hypotheses"]["H1"]
    assert (h1["state"], h1["reason"]) == ("not_tested", "too_few_problems")  # fewer than 30 hidden problems
    assert h1["non_inferiority"]["met"] is True  # the personal arm passes every pack task here
    h2 = written["hypotheses"]["H2"]
    assert (h2["state"], h2["reason"]) == ("not_testable", "no_lessons_or_ways_of_working")
    assert written["holm"]["adjusted"] == {"H1": None, "H2": None}
    assert written["arms"]["personal"]["splits"]["packs"]["runs"] == {"passed": 72}


def test_freeze_with_no_version_says_so(profile, folders, capsys):
    scratch, out = folders
    import shutil

    shutil.rmtree(profile / "learning_models" / "versions" / "v2")

    code = report.main(["--freeze", "--as-of", "2030-01-01", "--profile", str(profile), "--scratch", str(scratch),
                        "--out", str(out), "--yes"],
                       hooks=report.Hooks(prepare=lambda *_: pytest.fail("nothing to serve"), container=lambda: False,
                                          ask=_never_asked, installed=lambda: False))

    assert code == 0
    h1 = _written(out)["hypotheses"]["H1"]
    assert (h1["state"], h1["reason"], h1["superiority"]) == ("not_testable", "no_version", None)


def _problems(count: int, *, personal_passes: int) -> list[dict[str, Any]]:
    found = []
    for number in range(count):
        question = f"Problem {number}: {CANARY}"
        case = ReplayCase(f"dec_{number}", f"chat-{number}", f"chat-{number}", 10.0 + number, "Document lookups",
                          "exact", False, ({"type": "human", "data": {"content": question}},), None, "model:x:y",
                          Action(text="yes"), "captured", "exact_action", question)
        found.append({"id": case.decision_id, "kind_of_work": "Document lookups", "thread_id": case.thread_id,
                      "conversation_id": case.thread_id, "created_at": case.created_at, **case.to_content()})
    return found[:count]


def _freeze_result(monkeypatch, count: int, *, personal_passes: int) -> dict[str, Any]:
    class Vault:
        def hidden_tasks(self, *, asked_by, packs=None):
            return [task for task in visible_tasks() if task.pack == "document_lookups"][:4]

        def replay_tasks(self, *, asked_by):
            return _problems(count, personal_passes=personal_passes)

    monkeypatch.setattr(evaluation, "_vault", lambda _store=None: Vault())

    def respond(messages, _tools, model):
        number = int(str(messages[-1].content).split()[1].rstrip(":"))
        return AIMessage(content="yes" if model == "personal" and number < personal_passes else "no")

    return evaluation.freeze_check([Arm("start", model_ref="start"), Arm("personal", model_ref="personal")],
                                   as_of=1e9, asked_by="test", contrasts=[("start", "personal")],
                                   client=ScriptedClient(respond, local=True), runner=Packs(), meter=TokenMeter(10**9),
                                   store=LearningStore())


def _assembled(result: dict[str, Any]) -> dict[str, Any]:
    built = report.assemble_freeze(result, as_of=date(2026, 11, 2), versions=[], newest=None,
                                   prepared=report.Prepared({"start": "s", "personal": "p"}, 32768, "ollama"),
                                   refreshed={}, container=False, main_reference=False, lessons=0, behaviours=0,
                                   hours=0.1)
    built["run"] = {"on": "2026-11-03", "build": None, "build_clean": True, "script_sha256": "0" * 64,
                    "code_sha256": "1" * 64, "code_files": 1, "copy": {"files": 1, "bytes": 1, "links_skipped": 0},
                    "minutes": 1.0}
    report.validate(built, report.FREEZE)
    return built


def test_h1_is_tested_only_with_thirty_counted_problems(learning_on, monkeypatch):
    short = _assembled(_freeze_result(monkeypatch, 29, personal_passes=29))["hypotheses"]["H1"]
    enough = _assembled(_freeze_result(monkeypatch, 30, personal_passes=30))["hypotheses"]["H1"]

    assert (short["state"], short["reason"], short["supported"]) == ("not_tested", "too_few_problems", None)
    assert short["superiority"]["problems"] == 29 and short["superiority"]["p_holm"] is None
    assert (enough["state"], enough["reason"]) == ("tested", None)
    assert enough["superiority"]["problems"] == 30


def test_h1_is_judged_on_its_holm_adjusted_p_value_across_h1_and_h2(learning_on, monkeypatch):
    clear = _assembled(_freeze_result(monkeypatch, 30, personal_passes=30))
    h1 = clear["hypotheses"]["H1"]

    # Every problem better: p = 1/10,001 from the sign flips; H2 untested enters as 1, so H1's p doubles.
    assert h1["superiority"]["p_value"] == round(1 / 10_001, 6)
    assert h1["superiority"]["p_holm"] == clear["holm"]["adjusted"]["H1"] == round(2 / 10_001, 6)
    assert h1["superiority"]["met"] is True and h1["non_inferiority"]["met"] is True and h1["supported"] is True
    assert clear["holm"]["family"] == ["H1", "H2"] and clear["holm"]["adjusted"]["H2"] is None

    weak = _assembled(_freeze_result(monkeypatch, 30, personal_passes=4))["hypotheses"]["H1"]
    assert weak["state"] == "tested" and weak["superiority"]["met"] is False and weak["supported"] is False
    assert weak["superiority"]["p_holm"] == min(1.0, round(2 * weak["superiority"]["p_value"], 6))


# ── --thinking ──────────────────────────────────────────────────────────────


def _ablation_results() -> dict[str, Any]:
    pair = {"overall": {"problems": 40, "clusters": 30, "resamples": 10_000, "permutations": 10_000, "seed": 1,
                        "difference": -0.07, "low": -0.15, "high": -0.01, "p_value": 0.13, "exact": False,
                        "baseline": 0.2, "arm": 0.13, "enough": True, "interval_verdict": "worse",
                        "not_worse": False},
            "by_kind": {}}
    pair["by_kind"]["own_work"] = dict(pair["overall"])
    return {
        "build": CANARY, "started_at": f"{CANARY} 21:00",
        "rows": {"A": {"fit": 0, "rows_measured": 100, "too_long": 100, "examples": 0, "providers": [CANARY]},
                 "B": {"fit": 100, "rows_measured": 100, "too_long": 0, "examples": 90},
                 "seen_visible": [f"{CANARY}-task"]},
        "train": {"B": {"version": 3, "job": f"job_{CANARY}", "state": "evaluating", "examples": 90, "steps": 23,
                        "gpu_hours": 0.1, "error": None}},
        "serve": {"B": {"off": f"model:ollama:row-bot-personal-v3:{CANARY}"}},
        "compare": {"arms": {"B-off": f"model:ollama:row-bot-personal-v3:{CANARY}"}, "container": True,
                    "seconds": 3600, "repeats": {"visible": 3, "hidden": 3},
                    "per_arm": {"B-off": {"visible": {"seconds_per_run": 2.8, "tokens": 100, "by_kind": {CANARY: [1, 2]}}},
                                "B-on": {"visible": {"seconds_per_run": 4.5, "tokens": 120}},
                                f"{CANARY}-on": {"visible": {"seconds_per_run": 1, "tokens": 1}}},
                    "analysis": {"visible": {"pairs": {"B-on vs B-off": pair}},
                                 "hidden": {"pairs": {"B-on vs B-off": pair, f"{CANARY} vs B-off": pair},
                                            "combined_pairs": {"B-on vs B-off": pair}}}},
    }


def test_the_thinking_recheck_keeps_only_aggregates_of_the_ablation(profile, folders, capsys, monkeypatch):
    scratch, out = folders
    ran: dict[str, Any] = {}

    def ablation(copy, where):
        ran.update(copy=copy)
        (where / "l2w_ablation.json").write_text(json.dumps(_ablation_results()), encoding="utf-8")
        return 0

    monkeypatch.setattr(report, "run_ablation", ablation)

    code = _run(["--thinking", "--profile", str(profile), "--scratch", str(scratch), "--out", str(out), "--yes"])

    printed = capsys.readouterr()
    assert code == 0
    written = _written(out)
    _assert_private(printed.out + printed.err + json.dumps(written), profile, scratch, _ids(profile))
    report.validate(written, report.THINKING)
    assert Path(ran["copy"]).parent.parent == scratch
    own = written["thinking_on_vs_off"]["B"]["hidden"]["by_kind"]["own_work"]
    assert (own["difference"], own["low"], own["p_value"]) == (-0.07, -0.15, 0.13)
    assert set(written["arms"]) == {"B-off", "B-on"} and written["training"]["B"]["steps"] == 23
