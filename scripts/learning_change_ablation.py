"""L3b's ablation (paper, pre-registration §9): lessons on/off x ways of working on/off, and the harder split.

Phases (each resumable: pack results are kept per arm, task and run in the scratch folder's arm cache):

- ``harder``: the main model alone on the harder split (O31) and, for comparison, on the visible packs, through
  the real runtime (the evaluation worker), ``--repeats`` runs per task: how often it really fails there.
- ``twobytwo``: on one seeded person (a copy of S1's or L3a's scratch profile), the four arms **none**,
  **lessons**, **ways** and **both** (the person's drafted lessons as the memories recall finds, their drafted
  ways of working as the turn's overlay), at equal budget, paired, ``--repeats`` runs per arm on hidden
  problems: the person's hidden corrected problems (replay, the change as guidance), the packs' hidden tasks,
  and the visible and harder splits (real runtime). ``--model main`` runs GPT-6.1 Sol (the ChatGPT lane);
  ``--model <ref>`` a local model served by Row-Bot's own server (the personal model, ``--serve-context``).

Only aggregates are written (``--out``), with the pre-registered paired analysis from ``evaluation`` (mean
paired difference, 95% cluster-bootstrap interval, permutation p-value) and denominators. Refuses the everyday
profile and its backups. Stops at once on any usage or rate limit.

    uv run python scripts/learning_change_ablation.py harder --data <scratch>/harder --eval-profile <profile>
        --out <scratch>/out --repeats 3
    uv run python scripts/learning_change_ablation.py twobytwo --source <a seeded person's profile copy>
        --data <scratch>/<person> --eval-profile <profile> --out <scratch>/out --model main
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SEED = 20261023


def _say(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def _refuse(data: Path) -> None:
    everyday = (Path.home() / ".row-bot").resolve()
    resolved = data.resolve()
    if resolved == everyday or everyday in resolved.parents or "RowBot-backups" in str(resolved):
        raise SystemExit("Refused: this is the everyday profile or its backup.")


def boot(data: Path, profile: Path | None) -> tuple[str, Any]:
    """Bind Row-Bot to ``data`` (with the ChatGPT lane's sign-in in memory when ``profile`` is given)."""

    os.environ["ROW_BOT_DATA_DIR"] = str(data)
    os.environ["ROW_BOT_LEARNING_PREVIEW"] = "1"
    os.environ["ROW_BOT_DESKTOP_NOTIFICATIONS"] = "off"
    os.environ.setdefault("ROW_BOT_AUTO_START_OLLAMA", "0")
    sys.path.insert(0, str(ROOT))
    from row_bot import learning, models

    learning.invalidate()
    if learning.data_dir().resolve() != data.resolve():
        raise SystemExit(f"Row-Bot's data folder is {learning.data_dir()}, not {data}")
    if profile is None:
        return "", None
    from row_bot.providers import codex
    from tests.e2e.learning.seeded.live import SignIn
    from tests.e2e.live_eval import eval_profile

    sign_in = SignIn(profile)
    sign_in.install()
    model = eval_profile.choose_model([item.model_id for item in codex.fetch_codex_model_infos()])
    ref = f"model:codex:{model}"
    models.adopt_saved_default(ref)
    return ref, sign_in


class Limited(Exception):
    pass


def _check_limit(case: Any) -> None:
    detail = str(getattr(case, "detail", "") or "").lower()
    if getattr(case, "status", "") == "error" and any(word in detail for word in ("rate limit", "usage limit", "429",
                                                                                  "quota", "too many requests")):
        raise Limited(detail[:200])


def _worker(arms: list[Any], data: Path, ref: str, *, local: bool) -> tuple[Any, Any]:
    from row_bot.learning import eval_worker

    worker_arms = {arm.name: eval_worker.WorkerArm(arm.name, ref, local) for arm in arms}
    credentials, providers, unsupported = eval_worker.gather(list(worker_arms.values()))
    if unsupported:
        raise SystemExit(f"These arms can't be handed to the worker: {unsupported}")
    spec = {"arms": [{"name": arm.name, "model_ref": ref, "lessons": [list(item) for item in arm.lessons],
                      "ways": [list(item) for item in arm.ways]} for arm in arms],
            "tools": eval_worker.safe_tools(eval_worker.person_tools()), "providers": providers,
            "code_backend": "container"}
    worker = eval_worker.EvalWorker(spec, credentials=credentials, person_data=data)
    worker.start()
    return worker, eval_worker.RuntimeRunner(worker, worker_arms, record_usage=eval_worker.ledger_recorder())


def _summary(comparison: Any) -> dict[str, Any]:
    return {
        "arms": {item.arm.name: {"passed": item.passed, "total": item.total, "by_kind": item.by_kind,
                                 "tokens": item.measured_tokens, "cloud_tokens": item.measured_cloud_tokens,
                                 "seconds": round(item.seconds, 1), "ran": item.ran, "cached": item.cached,
                                 "causes": item.causes} for item in comparison.arms},
        "analysis": comparison.analysis, "repeats": comparison.repeats, "repeated": comparison.repeated,
        "stopped": comparison.stopped,
    }


def _write(out: Path, name: str, value: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    (out / name).write_text(json.dumps(value, indent=1, sort_keys=True, default=str), encoding="utf-8", newline="\n")
    _say(f"wrote {out / name}")


def phase_harder(args: argparse.Namespace) -> None:
    from row_bot.learning import evaluation, kinds
    from row_bot.learning.packs import harder, visible_tasks
    from row_bot.learning.sandbox import container_runtime
    from row_bot.learning.spending import TokenMeter
    from row_bot.learning.store import LearningStore

    data = Path(args.data).resolve()
    _refuse(data)
    data.mkdir(parents=True, exist_ok=True)
    ref, sign_in = boot(data, Path(args.eval_profile))
    from row_bot import learning

    learning.read_settings()
    container = bool(container_runtime())
    tasks = [task for task in harder.tasks() if container or task.verifier.kind != "code_tests"]
    if args.visible:
        per_pack: dict[str, int] = {}
        for task in visible_tasks():
            if not container and task.pack == "small_coding":
                continue
            per_pack[task.pack] = per_pack.get(task.pack, 0) + 1
            if per_pack[task.pack] <= args.visible:
                tasks.append(task)
    arm = evaluation.Arm("main", model_ref=ref, cache=True)
    cache = evaluation.ArmCache(data)
    meter = TokenMeter(int(args.cap))
    started = time.monotonic()
    done = [0]

    def counted(_arm: str, case: Any) -> None:
        done[0] += 1
        _check_limit(case)
        _say(f"{done[0]}/{len(tasks) * args.repeats}: {case.task_id} {case.status} {case.cause} "
             f"{case.tokens} tokens ({cache.hits} kept)")

    worker, runner = _worker([arm], data, ref, local=False)
    try:
        result = evaluation.compare([arm], tasks=tasks, runner=runner, cache=cache, meter=meter,
                                    store=LearningStore(data), save=False, on_case=counted,
                                    budget=evaluation.Budget(task_tokens=150_000), repeats=args.repeats,
                                    kind_key=kinds.from_label)
    finally:
        worker.stop()
        if sign_in is not None:
            sign_in.scrub()
    item = result.arms[0]
    hard = [case for case in item.cases if case.task_id.startswith(harder.PREFIX)]
    visible = [case for case in item.cases if not case.task_id.startswith(harder.PREFIX)]

    def rate(cases: list[Any]) -> dict[str, Any]:
        fractions = evaluation.pass_fractions(cases)
        counted_runs = [case for case in cases if case.counted]
        passed = sum(case.passed for case in counted_runs)
        low, high = None, None
        if counted_runs:
            half = evaluation.wilson_half_width(passed, len(counted_runs))
            share = passed / len(counted_runs)
            low, high = round(max(0.0, share - half), 4), round(min(1.0, share + half), 4)
        by_task = {task: round(value, 3) for task, (_kind, value) in sorted(fractions.items())}
        return {"tasks": len(fractions), "runs": len(counted_runs), "passed_runs": passed,
                "pass_rate": round(passed / len(counted_runs), 4) if counted_runs else None,
                "wilson_low": low, "wilson_high": high,
                "mean_task_pass_fraction": round(sum(by_task.values()) / len(by_task), 4) if by_task else None,
                "per_task": by_task,
                "causes": dict(sorted({cause: sum(1 for case in cases if case.cause == cause)
                                       for cause in {case.cause for case in cases if case.cause}}.items()))}

    _write(Path(args.out), "harder_baseline.json", {
        "model": ref, "repeats": args.repeats, "container": container, "minutes": round((time.monotonic() - started) / 60, 1),
        "harder": rate(hard), "visible": rate(visible) if visible else None,
        "tokens": item.measured_tokens, "cloud_tokens": item.measured_cloud_tokens, "stopped": result.stopped,
    })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="phase", required=True)
    harder_parser = sub.add_parser("harder")
    harder_parser.add_argument("--data", required=True)
    harder_parser.add_argument("--eval-profile", required=True)
    harder_parser.add_argument("--out", required=True)
    harder_parser.add_argument("--repeats", type=int, default=3)
    harder_parser.add_argument("--visible", type=int, default=0, help="visible tasks per pack to run beside it")
    harder_parser.add_argument("--cap", type=int, default=4_000_000, help="cloud tokens this phase may spend")
    args = parser.parse_args(argv)
    try:
        if args.phase == "harder":
            phase_harder(args)
    except Limited as error:
        _say(f"STOPPED: a usage or rate limit ({error}). Nothing more is sent.")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
